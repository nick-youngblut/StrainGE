#  Copyright (c) 2016-2019, Broad Institute, Inc. All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#
#  * Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
#  * Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
#  * Neither the name Broad Institute, Inc. nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
#  ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
#  LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
#  CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
#  SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
#  INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
#  CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
#  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
#  POSSIBILITY OF SUCH DAMAGE.
#

import csv
import sys
import json
import logging
import argparse
import functools
import itertools
import multiprocessing
from pathlib import Path
from collections import Counter

import numpy
import pysam
import skbio
import pandas
from skbio.stats.distance import DistanceMatrix

from strainge.variant_caller import (VariantCaller, Reference,
                                     analyze_repetitiveness)
from strainge.sample_compare import (SampleComparison, kimura_distance,
                                     count_ts_tv)
from strainge.io.variants import (call_data_from_hdf5, call_data_to_hdf5,
                                  boolean_array_to_bedfile,  write_vcf,
                                  generate_call_summary_tsv, array_to_wig)
from strainge.io.comparisons import (generate_compare_summary_tsv,
                                     generate_compare_details_tsv)
from strainge.io.utils import open_compressed, parse_straingst
from strainge.cli.registry import Subcommand
from strainge import cluster

logger = logging.getLogger()


class PrepareRefSubcommand(Subcommand):
    """
    Prepare a concatenated reference for StrainGR variant calling.
    """

    def register_arguments(self, subparser: argparse.ArgumentParser):
        io_group = subparser.add_argument_group(
            "I/O", "Arguments to specify input and output files."
        )
        io_group.add_argument(
            '-r', '--refs', nargs='*',
            help="Force inclusion of given reference genome in the "
                 "concatenated reference output (pre-clustering). The given "
                 "name should match a reference genome in the StrainGST "
                 "database. To be clear: the given argument should *not* be a "
                 "filename. See --path-template how this command finds the "
                 "corresponding FASTA files."
        )

        io_group.add_argument(
            '-s', '--straingst-files', type=Path, nargs='*',
            help="Read the list of StrainGST result files and collect all "
                 "reported strains to include in the concatenated output. "
                 "Use together with --path-template to specify how to "
                 "determine the correct filename."
        )

        io_group.add_argument(
            '-p', '--path-template', default="{ref}.fa",
            help='Specify how to determine the path to the FASTA file of a '
                 'reference strain as reported by StrainGST. This command '
                 'will replace "{ref}" with the strain name. Example: '
                 '"refs/{ref}.fa". Warning: in many shells { and } are '
                 'special characters. Make sure to use quotes. Default: '
                 '%(default)s.'
        )

        io_group.add_argument(
            '-o', '--output', type=Path, required=True,
            help="Output FASTA filename."
        )

        cluster_group = subparser.add_argument_group(
            "Clustering", "Options to change clustering behaviour."
        )

        cluster_group.add_argument(
            '-S', '--similarities', type=argparse.FileType('r'), default=None,
            help="Enable clustering of closely related reference genomes by "
                 "specifying the path to the k-mer similarity scores as "
                 "created at the StrainGST database construction step."
        )

        cluster_group.add_argument(
            '-t', '--threshold', type=float, default=0.7,
            help="K-mer clustering threshold, the default (%(default)s) is a "
                 "bit more lenient than the clustering step for database "
                 "construction, because for a concatenated reference you'll "
                 "want the included references not too closely related, "
                 "due to increased shared content."
        )

        mummer_group = subparser.add_argument_group(
            "MUMmer", "Settings for MUMmer, used to analyze the "
                      "repetitiveness of a concatenated reference."
        )

        mummer_group.add_argument(
            '-l', '--minmatch', type=int, default=250,
            help="Mininum exact match size. Default: %(default)d. For "
                 "best estimation that resembles StrainGR's 'lowmq' field, "
                 "set this to your library's average insert size."
        )

    def __call__(self, refs, straingst_files, path_template, output,
                 similarities, threshold, minmatch, *args, **kwargs):

        logger.info("Determining which reference strains to include...")
        refs = set(refs) if refs else set()
        straingst_counter = Counter()

        if straingst_files:
            logger.info("Reading all given StrainGST result files and "
                        "collecting reported reference genomes...")

            for fpath in straingst_files:
                logger.debug("Reading %s", fpath)
                with open(fpath) as f:
                    straingst_counter.update(strain['strain'] for strain in
                                             parse_straingst(f))

            logger.debug("StrainGST counts: %s", straingst_counter)

        refs = refs | straingst_counter.keys()
        logger.info("Found %d reference strains to include.", len(refs))

        logger.info("Checking file paths...")
        logger.info("Path template: %s", path_template)
        ref_paths = {}
        for ref in refs:
            target_path = Path(path_template.format(ref=ref))
            if not target_path.is_file():
                logger.error("%s does not exist!", target_path)
                return 1

            ref_paths[ref] = target_path

        if similarities:
            logger.info("Load k-mer similarity scores for clustering...")
            similarities = pandas.read_csv(similarities, sep='\t', comment='#')

            ix = (similarities['kmerset1'].isin(refs) &
                  similarities['kmerset2'].isin(refs))
            similarities = similarities[ix].set_index(['kmerset1', 'kmerset2'])
            similarities.sort_values('jaccard', ascending=False, inplace=True)
            labels = list(refs)

            logger.info("Pairwise k-mer similarities of genomes before "
                        "clustering:")
            with pandas.option_context("display.max_rows", None,
                                       "display.max_columns", None):
                print(similarities, file=sys.stderr)

            clusters = cluster.cluster_genomes(similarities, labels, threshold)

            orig_refs = refs
            refs = set()
            metric = straingst_counter if straingst_files else 'jaccard'
            for ix, sorted_entries in cluster.pick_representative(
                    clusters, similarities, metric=metric):
                if len(sorted_entries) > 1:
                    logger.info("The reference strains %s are too closely "
                                "related. Only keeping %s.", sorted_entries,
                                sorted_entries[0])
                refs.add(sorted_entries[0])

            logger.info("After clustering %d/%d reference strains remain.",
                        len(refs), len(orig_refs))

        logger.info("Creating concatenated reference...")

        concat_meta = {
            'contig_to_strain': {},
            'repetitiveness': {}
        }
        with output.open('w') as o:
            for ref in refs:
                with open_compressed(ref_paths[ref]) as f:
                    for record in skbio.io.read(f, "fasta", verify=False):
                        contig = record.metadata['id']
                        concat_meta['contig_to_strain'][contig] = ref

                        skbio.io.write(record, "fasta", o)

        logger.info("Wrote FASTA file to %s", output)
        logger.info("Analyzing repetitiveness of concatenated reference...")
        repeat_masks = analyze_repetitiveness(str(output), minmatch)

        with output.with_suffix('.repetitive.bed').open('w') as o:
            for contig, repeat_mask in repeat_masks.items():
                boolean_array_to_bedfile(repeat_mask, o, contig)

                frac_repetitive = repeat_mask.sum() / len(repeat_mask)
                strain = concat_meta['contig_to_strain'][contig]
                logger.info("%s, %s: %.1f%% repetitive content", strain,
                            contig, frac_repetitive * 100)
                concat_meta['repetitiveness'][contig] = frac_repetitive

        if max(concat_meta['repetitiveness'].values()) > 0.85:
            logger.warning("One of the strains has more than 85% repetitive "
                           "content, you may want to perform more broad "
                           "clustering, and lower the threshold.")

        with output.with_suffix('.meta.json').open('w') as o:
            json.dump(concat_meta, o, indent=2)

        logger.info("Done. Wrote concatenated reference metadata to %s",
                    output.with_suffix('.meta.json'))


def coverage_track(call_data, output_file, *args, **kwargs):
    logger.info("Writing 'coverage' Wiggle track...")
    for scaffold in call_data.scaffolds_data.values():
        array_to_wig(scaffold.coverage, output_file, scaffold.name)


def callable_track(call_data, output_file, min_size):
    logger.info("Writing 'callable' BED track...")
    for scaffold in call_data.scaffolds_data.values():
        boolean_array_to_bedfile(scaffold.strong > 0, output_file,
                                 scaffold.name, min_size)


def multimapped_track(call_data, output_file, *args, **kwargs):
    logger.info("Writing 'multimapped' Wiggle track...")
    for scaffold in call_data.scaffolds_data.values():
        array_to_wig(scaffold.lowmq_count, output_file, scaffold.name)


def lowmq_track(call_data, output_file, min_size):
    logger.info("Writing 'low mapping quality' BED track...")
    for scaffold in call_data.scaffolds_data.values():
        boolean_array_to_bedfile(scaffold.lowmq, output_file, scaffold.name,
                                 min_size)


def bad_track(call_data, output_file, *args, **kwargs):
    logger.info("Writing 'bad reads' Wiggle track...")
    for scaffold in call_data.scaffolds_data.values():
        array_to_wig(scaffold.bad, output_file, scaffold.name)


def high_coverage_track(call_data, output_file, min_size):
    logger.info("Writing 'high coverage' BED track...")
    for scaffold in call_data.scaffolds_data.values():
        boolean_array_to_bedfile(scaffold.high_coverage, output_file,
                                 scaffold.name, min_size)


def gaps_track(call_data, output_file, *args, **kwargs):
    logger.info("Writing 'gaps' BED track...")
    writer = csv.writer(output_file, delimiter='\t', lineterminator='\n')
    for scaffold in call_data.scaffolds_data.values():
        for gap in scaffold.gaps:
            writer.writerow((scaffold.name, gap.start, gap.end))


TRACKS = {
    "coverage": (".coverage.wig", coverage_track),
    "callable": (".callable.bed", callable_track),
    "multimapped": (".multimapped.wig", multimapped_track),
    "lowmq": (".lowmq.bed", lowmq_track),
    "bad": (".bad.wig", bad_track),
    "high_coverage": (".high_coverage.bed", high_coverage_track),
    "gaps": (".gaps.bed", gaps_track)
}


def write_tracks(call_data, tracks, prefix, min_size=1):
    """
    Write the requested tracks to their corresponding files.

    The filename suffixes are hardcoded, the final file path is based on the
    given `prefix`.

    Parameters
    ----------
    call_data
    tracks : set
    prefix : Path
    min_size : int
    """
    if "all" in tracks:
        tracks = TRACKS.keys()

    unknown_tracks = tracks - TRACKS.keys()
    if unknown_tracks:
        logger.warning("Ignoring unknown tracks: %s", ",".join(unknown_tracks))

    tracks = tracks & TRACKS.keys()

    for track in tracks:
        suffix, func = TRACKS[track]

        with prefix.with_suffix(suffix).open("w") as f:
            func(call_data, f, min_size)


class CallSubcommand(Subcommand):
    """
    StrainGR: strain-aware variant caller for metagenomic samples

    This command analyzes
    """
    def register_arguments(self, subparser: argparse.ArgumentParser):
        subparser.add_argument(
            'reference',
            help="Reference FASTA file. Can be GZIP compressed."
        )

        subparser.add_argument(
            'sample',
            help="BAM file with the aligned reads of the sample against the "
                 "reference"
        )

        call_qc_group = subparser.add_argument_group(
            'Quality control',
            'Options which determine which reads to consider, when base or '
            'read mapping qualities are high enough for calling, etc.'
        )
        call_qc_group.add_argument(
            '-Q', '--min-qual', type=int, default=5,
            help="Minimum quality for a base to be considered. Default: %("
                 "default)d."
        )
        call_qc_group.add_argument(
            '-P', '--min-pileup-qual', type=int, default=50,
            help="Minimum sum of qualities for an allele to be trusted."
                 "for variant calling. Default: %(default)d."
        )
        call_qc_group.add_argument(
            '-F', '--min-qual-frac', type=float, default=0.1,
            help="Minimum fraction of the reads in the pileup required to "
                 "confirm an allele (fractions are base quality weighted). "
                 "Default: %(default)g"
        )
        call_qc_group.add_argument(
            '-M', '--min-mapping-qual', type=int, default=5,
            help="Minimum mapping quality of the whole read to be considered. "
                 "Default: %(default)d."
        )
        call_qc_group.add_argument(
            '-N', '--max-mismatches', type=int, default=0,
            help="Ignore alignments with a higher number of mismatches than "
                 "the given threshold. A value of 0 disables this "
                 "check. Default: %(default)d."
        )
        call_qc_group.add_argument(
            '-G', '--min-gap', type=int, default=2000,
            help="Minimum size of gap to be considered as such. Default: "
                 "2000. Will be automatically scaled depending on coverage."
        )

        call_out_group = subparser.add_argument_group(
            "Output formats",
            "Options for writing the results to different file formats."
        )

        call_out_group.add_argument(
            '-o', '--hdf5-out', required=True, metavar='FILE',
            help="Output StrainGR variant calling data to the given HDF5 "
                 "file. Required."
        )
        call_out_group.add_argument(
            '-s', '--summary', type=argparse.FileType('w'), required=False,
            metavar='FILE', default=sys.stdout,
            help="Output a TSV with a summary of variant calling statistics "
                 "to the given file. Defaults to stdout."
        )
        call_out_group.add_argument(
            '-V', '--vcf', type=argparse.FileType('w'), required=False,
            default=None, metavar='FILE',
            help="Output a VCF file with SNP's. Please be aware that "
                 "we do not have a good insertion/deletion calling mechanism, "
                 "but some information on possible indels is written to the "
                 "VCF file."
        )
        call_out_group.add_argument(
            '--verbose-vcf', type=int, default=0, metavar='LEVEL',
            help="To be used with --vcf. Increase the verboseness of the "
                 "generated VCF. By default it only outputs strong SNPs. A "
                 "value of 1 will also output any weak calls. If set to 2, "
                 "it will include an entry for every position in the "
                 "reference, even if no other base than the reference is "
                 "observed."
        )
        call_out_group.add_argument(
            '-t', '--tracks', action="append", default=[],
            help="Write track files that can be visualized in a genome "
                 "viewer, use this option multiple times to generate "
                 "multiple track types. Use 'all' to generate all tracks. "
                 "Available track types: {}".format(
                     ", ".join(TRACKS.keys())
            )
        )
        call_out_group.add_argument(
            '--track-min-size', type=int, required=False, default=1,
            help="For all tracks to generate, only include features ("
                 "regions)  of at least the given size. Default: %(default)d."
        )

    def __call__(self, reference, sample,
                 min_qual, min_pileup_qual, min_qual_frac,
                 min_mapping_qual, min_gap, max_mismatches,
                 summary=None, hdf5_out=None,
                 vcf=None, verbose_vcf=False,
                 tracks=None, track_min_size=1, **kwargs):
        """Call variants in a mixed-strain sample."""

        logger.info("Loading reference %s...", reference)
        reference = Reference(reference)
        logger.info("Reference length: %d", reference.length)
        sample_bam = pysam.AlignmentFile(sample)

        logger.info("Start analyzing aligned reads...")
        caller = VariantCaller(min_qual, min_pileup_qual, min_qual_frac,
                               min_mapping_qual, min_gap, max_mismatches)

        call_data = caller.process(reference, sample_bam)

        # Output call datasets to HDF5
        logger.info("Writing data to HDF5 file %s...", hdf5_out)
        call_data_to_hdf5(call_data, hdf5_out)

        if summary:
            # Output a summary TSV
            if summary != sys.stdout:
                logger.info("Writing summary to %s", summary.name)

            generate_call_summary_tsv(call_data, summary)

        if vcf:
            logger.info("Generating VCF file...")
            write_vcf(call_data, vcf, verbose_vcf)

        if tracks:
            write_tracks(call_data, set(tracks), Path(hdf5_out),
                         track_min_size)

        logger.info("Done.")


class ViewSubcommand(Subcommand):
    """
    View call statistics stored in a HDF5 file and output results to
    different file formats
    """

    def register_arguments(self, subparser: argparse.ArgumentParser):
        subparser.add_argument(
            'hdf5',
            help="HDF5 file with StrainGR call statistics."
        )

        subparser.add_argument(
            '-s', '--summary', type=argparse.FileType('w'), required=False,
            default=sys.stdout, metavar='FILE',
            help="Output a TSV with a summary of variant calling statistics "
                 "to the given file."
        )
        subparser.add_argument(
            '-t', '--tracks', action="append", default=[],
            help="Write track files that can be visualized in a genome "
                 "viewer, use this option multiple times to generate "
                 "multiple track types. Use 'all' to generate all tracks. "
                 "Available track types: {}".format(
                ", ".join(TRACKS.keys())
            )
        )
        subparser.add_argument(
            '--track-min-size', type=int, required=False, default=1,
            help="For all --track-* options above, only include features ("
                 "regions) of at least the given size. Default: %(default)d."
        )

        subparser.add_argument(
            '-V', '--vcf', type=argparse.FileType('w'), required=False,
            default=None, metavar='FILE',
            help="Output a VCF file with SNP's. Please be aware that "
                 "we do not have a good insertion/deletion calling mechanism, "
                 "but some information on possible indels is written to the "
                 "VCF file."
        )
        subparser.add_argument(
            '--verbose-vcf', type=int, default=0, metavar='LEVEL',
            help="To be used with --vcf. Increase the verboseness of the "
                 "generated VCF. By default it only outputs strong SNPs. A "
                 "value of 1 will also output any weak calls. If set to 2, "
                 "it will include an entry for every position in the "
                 "reference, even if no other base than the reference is "
                 "observed."
        )

    def __call__(self, hdf5, summary=None, tracks=None, track_min_size=1,
                 vcf=None, verbose_vcf=False, **kwargs):
        """View and output the StrainGR calling results in different file
        formats."""
        logger.info("Loading data from HDF5 file %s", hdf5)
        call_data = call_data_from_hdf5(hdf5)

        if summary:
            # Output a summary TSV
            if summary != sys.stdout:
                logger.info("Writing summary to %s", summary.name)
            generate_call_summary_tsv(call_data, summary)

        if tracks:
            tracks = set(tracks)

            write_tracks(call_data, tracks, Path(hdf5), track_min_size)

        if vcf:
            logger.info("Generating VCF file...")
            write_vcf(call_data, vcf, verbose_vcf)

        logger.info("Done.")


class CompareSubCommand(Subcommand):
    """
    Compare strains and variant calls in two different samples. Reads of
    both samples must be aligned to the same reference.

    It's possible to generate a TSV with summary stats as well as a file
    with more detailed information on which alleles are called at what
    positions.
    """

    def register_arguments(self, subparser: argparse.ArgumentParser):
        subparser.add_argument(
            'samples', nargs='+', metavar='SAMPLE_HDF5', type=Path,
            help="HDF5 files with variant calling data for each sample. "
                 "Number of samples should be exactly two, except when used "
                 "with --baseline."
        )

        subparser.add_argument(
            '-o', '--summary-out', type=argparse.FileType('w'),
            default=sys.stdout,
            help="Output file for summary statistics. Defaults to stdout."
        )

        subparser.add_argument(
            '-d', '--details-out', type=argparse.FileType('w'), default=None,
            help="Output file for detailed base level differences between "
                 "samples (optional)."
        )
        subparser.add_argument(
            '-V', '--verbose-details', action="store_true", default=False,
            help="Output detailed information for every position in the "
                 "genome instead of only for positions where alleles differ."
        )

        group = subparser.add_mutually_exclusive_group()

        group.add_argument(
            '-a', '--all-vs-all', action="store_true", required=False,
            help="Perform all-vs-all pairwise comparisons between the given "
                 "samples. Can't be used together with --baseline."
        )

        group.add_argument(
            '-b', '--baseline', default="", required=False, type=Path,
            help="Path to a sample to use as baseline, and compare all other "
                 "given samples to this one. Outputs a shell script that "
                 "runs all individual pairwise comparisons. Can't be used "
                 "together with --all-vs-all."
        )

        subparser.add_argument(
            '-D', '--output-dir', default="", required=False, type=Path,
            help="The output directory of all comparison files when using "
                 "--baseline or --all-vs-all."
        )

    def __call__(self, samples, summary_out=None, details_out=None,
                 verbose_details=False, baseline=None, all_vs_all=False,
                 output_dir="", *args, **kwargs):
        if baseline and not baseline.is_file() and not baseline == Path(""):
            logger.error("Baseline %s does not exists.", baseline)
            return 1
        elif baseline and baseline.is_file() or all_vs_all:
            output_dir = Path(output_dir)

            if all_vs_all:
                pairs = itertools.combinations(samples, 2)
            else:
                pairs = ((baseline, sample) for sample in samples
                         if sample != baseline)

            for sample1, sample2 in pairs:
                fname_base = f"{sample1.stem}.vs.{sample2.stem}"
                summary_file = output_dir / f"{fname_base}.summary.tsv"
                details_file = output_dir / f"{fname_base}.details.tsv"
                print(sys.argv[0], "compare",
                      "-o", summary_file,
                      "-d", details_file,
                      "-V" if verbose_details else "",
                      sample1, sample2)
        else:
            if len(samples) != 2:
                logger.error("The number of samples given should be exactly "
                             "two. To compare multiple samples against a "
                             "single baseline, use --baseline.")

                return 1

            logger.info("Comparing sample %s vs %s", samples[0].stem,
                        samples[1].stem)

            logger.info("Loading sample 1 %s", samples[0].stem)
            call_data1 = call_data_from_hdf5(samples[0])
            logger.info("Loading sample 2 %s", samples[1].stem)
            call_data2 = call_data_from_hdf5(samples[1])

            comparison = SampleComparison(call_data1, call_data2)

            generate_compare_summary_tsv(comparison, summary_out)

            if details_out:
                logger.info("Generating details file...")
                generate_compare_details_tsv(details_out, call_data1,
                                             call_data2, verbose_details)

            logger.info("Done.")


class DistSubcommand(Subcommand):
    """
    Build a distance matrix using Kimura's two parameter model, for all strains
    in samples close to a selected reference genome.
    """

    def register_arguments(self, subparser: argparse.ArgumentParser):
        subparser.add_argument(
            '-r', '--reference', type=Path,
            help="The reference genome to base the tree on."
        )

        subparser.add_argument(
            '-x', '--min-coverage', type=float, required=False, default=0.5,
            help="Minimum coverage of the reference genome to consider a "
                 "sample. Default %(default)sx."
        )

        subparser.add_argument(
            '-a', '--min-abundance', type=float, required=False, default=0.05,
            help="Minimum abundance fraction of this strain in a sample. "
                 "Default %(default)s."
        )

        subparser.add_argument(
            '-p', '--processes', type=int, default=2,
            help="Number of parallel processes to start. Default %(default)s."
        )

        subparser.add_argument(
            '-o', '--output', type=argparse.FileType('w'), default=sys.stdout,
            help="Output filename. Defaults to stdout."
        )

        subparser.add_argument(
            'samples', nargs='+', type=Path,
            help="StrainGR call data HDF5 file for each sample."
        )

    def _do_ref_compare(self, sample, ref_contigs, min_coverage,
                        min_abundance):
        try:
            ref, sample = sample
            logger.info("Loading %s", sample)
            call_data = call_data_from_hdf5(sample)

            total_length = sum(call_data.scaffolds_data[s].length for s in
                               ref_contigs if s in call_data.scaffolds_data)

            if total_length == 0:
                # Strain not present in this sample
                logger.info("Sample does not contain %s", ref)
                return ref, sample, -1

            # Sometimes our concatenated StrainGR reference does not include
            # all scaffolds of the original reference, for example small
            # plasmids are often filtered. Only analyse reference contigs that
            # are actually present in our call data.
            ref_contigs = {r for r in ref_contigs if r in
                           call_data.scaffolds_data}

            coverage = sum(
                call_data.scaffolds_data[s].mean_coverage *
                call_data.scaffolds_data[s].length
                for s in ref_contigs
            ) / total_length

            if coverage < min_coverage:
                logger.info("Skipping %s, coverage %.2f too low.", sample,
                            coverage)
                return ref, sample, -1

            abundance = (sum(call_data.scaffolds_data[s].read_count
                             for s in ref_contigs)
                         / call_data.uniquely_mapped_reads)

            if abundance < min_abundance:
                logger.info("Skipping %s, abundance %.2f too low.", sample,
                            abundance)
                return ref, sample, -1

            logger.info("Comparing %s to %s (mean coverage: %.2f, abundance: "
                        "%.2f)", sample, ref, coverage, abundance)

            total_singles = 0
            total_ts = 0
            total_tv = 0
            for scaffold in call_data.scaffolds_data.values():
                if scaffold.name not in ref_contigs:
                    continue

                singles = (scaffold.strong & (scaffold.strong - 1)) == 0
                singles &= scaffold.strong > 0
                total_singles += numpy.count_nonzero(singles)

                snps = scaffold.strong & ~scaffold.refmask
                single_snps = (singles & snps).astype(bool)
                logger.info("Scaffold %s has %d single allele snps.",
                            scaffold.name, numpy.count_nonzero(single_snps))

                transitions, transversions = count_ts_tv(
                    scaffold.refmask[single_snps],
                    scaffold.strong[single_snps]
                )

                total_ts += transitions
                total_tv += transversions

            ts_pct = total_ts / total_singles
            tv_pct = total_tv / total_singles

            logger.info("Singles: %d, ts: %d (%.2f%%), tv: %d (%.2f%%)",
                        total_singles, total_ts, ts_pct, total_tv, tv_pct)

            return ref, sample, kimura_distance(ts_pct, tv_pct)
        except KeyboardInterrupt:
            pass

    def _do_sample_compare(self, samples, ref_contigs):
        try:
            sample1, sample2 = samples
            logger.info("Comparing %s to %s", sample1, sample2)

            call_data1 = call_data_from_hdf5(sample1)
            call_data2 = call_data_from_hdf5(sample2)

            comparison = SampleComparison(call_data1, call_data2)
            metrics = {k: v for k, v in comparison.metrics.items()
                       if k in ref_contigs}
            total_single = sum(m['single'] for m in metrics.values())

            if total_single == 0:
                return sample1, sample2, 0.0

            ts_pct = sum(m['transitionsPct'] * m['single'] / 100
                         for k, m in metrics.items())
            ts_pct /= total_single

            tv_pct = sum(m['transversionsPct'] * m['single'] / 100
                         for m in metrics.values())
            tv_pct /= total_single

            return sample1, sample2, kimura_distance(ts_pct, tv_pct)
        except KeyboardInterrupt:
            pass

    def __call__(self, reference, samples, min_coverage, min_abundance,
                 processes, output, *args, **kwargs):
        # Check which contigs belong to this reference genome
        logger.info("Building tree for reference %s", reference.stem)
        logger.info("Loading reference scaffold IDs...")
        with open_compressed(reference) as f:
            ref_contigs = {r.metadata['id']
                           for r in skbio.io.read(f, 'fasta', verify=False)}

        logger.info("Inspecting scaffolds %s", ref_contigs)

        with multiprocessing.Pool(processes) as p:
            logger.info("Comparing samples to reference...")
            ref_scores = list(p.imap_unordered(
                functools.partial(self._do_ref_compare,
                                  ref_contigs=ref_contigs,
                                  min_coverage=min_coverage,
                                  min_abundance=min_abundance),
                ((reference.stem, sample) for sample in samples),
                chunksize=2**4
            ))

            exclude_samples = set(s[1] for s in ref_scores if s[2] == -1)
            for sample in exclude_samples:
                logger.info("Excluding sample %s because the reference has "
                            "too low coverage or too low abundance.", sample)

            samples = [s for s in samples if s not in exclude_samples]
            sample_ix = {s.stem: i+1 for i, s in enumerate(samples)}
            sample_ix[reference.stem] = 0
            names = [reference.stem] + [s.stem for s in samples]

            dm_array = numpy.zeros((len(names), len(names)))

            for _, sample, score in ref_scores:
                if sample in exclude_samples:
                    continue

                i = sample_ix[reference.stem]
                j = sample_ix[sample.stem]

                dm_array[i, j] = score
                dm_array[j, i] = score

            logger.info("Comparing samples to eachother...")
            pair_iter = itertools.combinations(samples, 2)
            sample_scores = list(p.imap_unordered(
                functools.partial(self._do_sample_compare,
                                  ref_contigs=ref_contigs),
                pair_iter,
                chunksize=2**4
            ))

            for sample1, sample2, score in sample_scores:
                i = sample_ix[sample1.stem]
                j = sample_ix[sample2.stem]

                dm_array[i, j] = score
                dm_array[j, i] = score

        logger.info("Writing distance matrix...")
        dm = DistanceMatrix(dm_array, names)
        dm.write(output)

        logger.info("Done.")


class TreeSubcommand(Subcommand):
    """
    Build an approximate phylogenetic tree based on Kimura's two parameter
    model, for all strains close to a selected reference genome.
    """

    def register_arguments(self, subparser: argparse.ArgumentParser):
        subparser.add_argument(
            'distance_matrix', type=argparse.FileType('r'),
            help="The path to the distance matrix TSV, as created by `straingr"
                 " dist`."
        )

        subparser.add_argument(
            '-o', '--output', type=argparse.FileType('w'), default=sys.stdout,
            help="Output filename. Defaults to stdout."
        )

    def __call__(self, distance_matrix, output, verbose, *args, **kwargs):
        logger.info("Loading distance matrix...")
        dm = DistanceMatrix.read(distance_matrix)

        logger.info("Building tree...")
        tree = skbio.tree.nj(dm)
        tree = tree.root_at_midpoint()

        if verbose > 0:
            logger.info("Approximate tree using neighbour joining:\n%s",
                        tree.ascii_art())

        tree.write(output, format='newick')
        logger.info("Done.")
