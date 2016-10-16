import sys
import gzip
import bz2
from Bio import SeqIO
import numpy as np
import matplotlib.pyplot as plt
import kmerizer

# A random 64-bit number used in hashing function
HASH_BITS = 0x29679e096c8c07bf

def openSeqFile(fileName):
    """
    Open a sequence file with SeqIO; can be fasta or fastq with optional gz or bz2 compression.
    Assumes fasta unless ".fastq" or ".fq" in the file name.
    :param fileName:
    :return: SeqIO.parse object
    """

    components = fileName.split('.')
    if "bz2" in components:
        file = bz2.BZ2File(fileName, 'r')
    elif "gz" in components:
        file = gzip.GzipFile(fileName, 'r')
        SeqIO.parse(file, "fastq")
    if "fastq" in components or "fq" in components:
        fileType = "fastq"
    else:
        fileType = "fasta"
    return SeqIO.parse(file, fileType)

class KmerSet:
    """
    Holds array of kmers and their associated counts & stats.
    """

    def __init__(self, k):
        self.k = k
        # data arrays
        self.kmers = None
        self.counts = None
        self.fingerprint = None
        # stats
        self.nSeqs = 0
        self.nBases = 0
        self.nKmers = 0

    def kmerizeFile(self, fileName, batchSize = 100000000, verbose = True):
        seqFile = openSeqFile(fileName)
        batch = np.empty(batchSize, dtype=np.uint64)

        nSeqs = 0
        nBases = 0
        nKmers = 0
        nBatch = 0 # kmers in this batch

        for seq in seqFile:
            nSeqs += 1
            seqLength = len(seq)
            nBases += seqLength
            if nKmers + seqLength > batchSize:
                self.processBatch(batch, nSeqs, nBases, nKmers, verbose)
                nSeqs = 0
                nBases = 0
                nKmers = 0
            nKmers += kmerizer.kmerize_into_array(self.k, str(seq.seq), batch, nKmers)
        seqFile.close()
        self.processBatch(batch, nSeqs, nBases, nKmers, verbose)

    def processBatch(self, batch, nseqs, nbases, nkmers, verbose):
        self.nSeqs += nseqs
        self.nBases += nbases
        self.nKmers += nkmers

        newKmers, newCounts = np.unique(batch[:nkmers], return_counts=True)

        if type(self.kmers) == type(None):
            self.kmers = newKmers
            self.counts = newCounts
        else:
            self.kmers, self.counts = kmerizer.merge_counts(self.kmers, self.counts, newKmers, newCounts)

        if verbose:
            self.printStats()

    def printStats(self):
        print 'Seqs:', self.nSeqs, 'Bases:', self.nBases, 'Kmers:', self.nKmers, \
            'Distinct:', self.kmers.size, 'Singletons:', np.count_nonzero(self.counts == 1)

    def hashKmers(self):
        k = self.k
        mask = (1 << (2 * k)) - 1
        hashedKmers = self.kmers >> k
        hashedKmers |= self.kmers << k
        hashedKmers ^= HASH_BITS
        hashedKmers &= mask
        hashedKmers.sort()
        return hashedKmers

    def minHash(self, nkmers = 10000):
        self.fingerprint = self.hashKmers()[:nkmers]
        return self.fingerprint

    def plotSpectrum(self, fileName = None, maxFreq = None):
        # to get kmer profile, count the counts!
        spectrum = np.unique(counts, return_counts=True)
        plt.semilogy(spectrum[0], spectrum[1])
        plt.grid = True
        if maxFreq:
            plt.xlim(0, maxFreq)
        plt.xlabel("Kmer Frequency")
        plt.ylabel("Number of Kmers")
        plt.suptitle = "Kmer Spectrum (K=%d)" % (options.k,)
        if fileName:
            plt.savefig(fileName)
        else:
            plt.show()

    def save(self, fileName, compress = False):
        kwargs = {'kmers': self.kmers, 'counts': self.counts}
        if type(self.fingerprint) != type(None):
            kwargs['fingerprint'] = self.fingerprint
        if compress:
            func = np.savez_compressed
        else:
            func = np.savez
        func(fileName, **kwargs)








