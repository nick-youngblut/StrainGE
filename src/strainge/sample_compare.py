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

import numpy
from intervaltree import IntervalTree

from strainge.utils import pct


class SampleComparison:
    """
    This class compares variant calls in two different samples, and gives
    statistics on how similar the strains are.
    """

    def __init__(self, call_data1, call_data2):
        """
        Compare the variant call data from two samples.

        Parameters
        ----------
        call_data1 : strainge.variant_caller.VariantCallData
            Variant call data of sample 1
        call_data2 : strainge.variant_caller.VariantCallData
            Variant call data of sample 2
        """

        self.metrics = {}

        self.sample1 = call_data1
        self.sample2 = call_data2

        for scaffoldA, scaffoldB in zip(call_data1.scaffolds_data.values(),
                                        call_data2.scaffolds_data.values()):
            assert scaffoldA.name == scaffoldB.name
            assert scaffoldA.length == scaffoldB.length

            self.metrics[scaffoldA.name] = self._do_compare(scaffoldA,
                                                            scaffoldB)
            self.metrics[scaffoldA.name].update(self.compare_gaps(scaffoldA,
                                                                  scaffoldB))

    def _do_compare(self, a, b):
        """

        Parameters
        ----------
        a : strainge.variant_caller.ScaffoldCallData
        b : strainge.variant_caller.ScaffoldCallData

        Returns
        -------

        """
        # common locations where both have a call
        common, common_cnt, common_pct = self.compare_thing(
            numpy.ones_like(a.refmask), numpy.logical_and(a.strong, b.strong))

        # locations where both have only a single allele called
        single_a = (a.strong & (a.strong - 1)) == 0
        single_b = (b.strong & (b.strong - 1)) == 0
        singles, single_cnt, single_pct = self.compare_thing(
            common, single_a & single_b)

        # locations where both have only a single allele called
        single_agree, single_agree_cnt, single_agree_pct = self.compare_thing(
            singles, a.strong == b.strong)

        # common locations where either has a variant from reference
        variants, variant_cnt, variant_pct = self.compare_thing(
            common, ((a.strong | b.strong) & ~a.refmask) > 0)

        # variant locations where both have a shared variant
        common_var, common_var_cnt, common_var_pct = self.compare_thing(
            variants, (a.strong & b.strong) > 0)

        # variant locations where both agree
        var_agree, var_agree_cnt, var_agree_pct = self.compare_thing(
            variants, a.strong == b.strong)

        # variant in a but not b
        a_not_b, a_not_b_cnt, a_not_b_pct = self.compare_thing(
            variants, (a.strong & ~b.strong & ~a.refmask) > 0)

        # variant in a but not b weakly
        a_not_bweak, a_not_bweak_cnt, a_not_bweak_pct = self.compare_thing(
            variants, (a.strong & ~b.weak & ~a.refmask) > 0)

        # variant in b not a
        b_not_a, b_not_a_cnt, b_not_a_pct = self.compare_thing(
            variants, (b.strong & ~a.strong & ~a.refmask) > 0)

        # variant in b not a weakly
        b_not_aweak, b_not_aweak_cnt, b_not_aweak_pct = self.compare_thing(
            variants, (b.strong & ~a.weak & ~a.refmask) > 0)

        return {
            "common": common_cnt,
            "commonPct": common_pct,
            "single": single_cnt,
            "singlePct": single_pct,
            "singleAgree": single_agree_cnt,
            "singleAgreePct": single_agree_pct,
            "variants": variant_cnt,
            "variantPct": variant_pct,
            "commonVariant": common_var_cnt,
            "commonVariantPct": common_var_pct,
            "variantAgree": var_agree_cnt,
            "variantAgreePct": var_agree_pct,
            "AnotB": a_not_b_cnt,
            "AnotBpct": a_not_b_pct,
            "AnotBweak": a_not_bweak_cnt,
            "AnotBweakPct": a_not_bweak_pct,
            "BnotA": b_not_a_cnt,
            "BnotApct": b_not_a_pct,
            "BnotAweak": b_not_aweak_cnt,
            "BnotAweakPct": b_not_aweak_pct
        }

    def compare_thing(self, common, thing):
        """
        Computes occurrence of a condition within a set, and returns those
        stats.

        :param common: flag for locations to consider
        :param thing: condition we're looking for in common

        :return: array where command and condition are true, count of that,
                 and percentage with respect to common
        """
        common_cnt = numpy.count_nonzero(common)
        common_things = numpy.logical_and(common, thing)
        common_things_cnt = numpy.count_nonzero(common_things)
        percent = pct(common_things_cnt, common_cnt)

        return common_things, common_things_cnt, percent

    def compare_gaps(self, a, b):
        """
        More stats, this time about uncovered regions in common.

        Parameters
        ----------
        a : strainge.variant_caller.ScaffoldCallData
        b : strainge.variant_caller.ScaffoldCallData
        """

        a_length = sum(g.length for g in a.gaps)
        b_length = sum(g.length for g in b.gaps)

        gap_tree_a = IntervalTree.from_tuples(
            (g.start, g.end, g) for g in a.gaps
        )
        gap_tree_b = IntervalTree.from_tuples(
            (g.start, g.end, g) for g in b.gaps
        )

        a_shared = [g for g in b.gaps if gap_tree_a[g.start:g.end]]
        b_shared = [g for g in a.gaps if gap_tree_b[g.start:g.end]]

        a_shared_length = sum(g.length for g in a_shared)
        b_shared_length = sum(g.length for g in b_shared)

        return {
            "Alength": a_length,
            "AsharedGaps": a_shared_length,
            "AgapPct": pct(a_shared_length, a_length),
            "Blength": b_length,
            "BsharedGaps": b_shared_length,
            "BgapPct": pct(b_shared_length, b_length)
        }