import os
import traceback
import time
import argparse
import numpy as np
import pandas as pd
from datetime import datetime
from functools import wraps
from collections import defaultdict
from scipy.sparse import load_npz

from utils.relatedness import LOCOpreds
from utils.null import NullModel
from utils.vsettest import VariantSetTest
from utils.utils import *


"""
type I error:
1. load raw genotype data by each chr
2. generate genes for each cMAC bin from chrs by proportion
3. count false positives at 2.5e-6

power:
1. load raw genotype data by each chr
2. generate gene for each cMAC bin using causal index for each chr
3. count true positives at 2.5e-6

"""


def log_execution_time(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.perf_counter()
        result = func(*args, **kwargs)
        elapsed_time = time.perf_counter() - start_time
        log.info(f"{func.__name__} executed in {elapsed_time:.4f}s")
        return result
    return wrapper


class RVsimulation:
    """
    Doing simulation for RVRVA

    """

    @log_execution_time
    def __init__(
            self, 
            covar, 
            sparse_genotype_dict, 
            chr_gene_numeric_idxs, 
            maf_dict, 
            mac_dict, 
            sig_thresh,
        ):
        """
        Parameters:
        ------------
        null_model: a NullModel instance
        sparse_genotype_dict: a dict of sparse genotype per chr
        chr_gene_numeric_idxs: a dict of gene idxs per chr per bin
        maf_dict: a dict of maf per chr
        mac_dict: a dict of mac per chr
        perm: an instance of PermDistribution
        sig_thresh: significant threshold
        resid_ldr_dict: a dict resid_ldr per chr

        """
        self.covar = covar
        self.n_subs, self.n_covars = covar.shape
        self.sparse_genotype_dict = sparse_genotype_dict # chr
        self.chr_gene_numeric_idxs = chr_gene_numeric_idxs # chr-bin
        self.maf_dict = maf_dict # chr
        self.mac_dict = mac_dict # chr
        self.all_bins = [(2,2), (3,3), (4,4), (5,5), (6,7), (8,9),
                         (10,11), (12,14), (15,20), (21,30), (31,60), 
                         (61,100), (101,500), (501,1000)]
        self.sig_thresh = sig_thresh
        self.logger = logging.getLogger(__name__)

        self.vset_ld_dict = self._get_ld_matrix() # chr
        self.vset_half_covar_proj_dict = self._get_vset_half_covar_proj() # chr
        self.chr_cov_mat_dict = self._get_cov_mat() # chr-bin

    def _get_ld_matrix(self):
        vset_ld_dict = dict()
        for chr, vset in self.sparse_genotype_dict.items():
            vset = vset.astype(np.uint16)
            vset_ld = vset @ vset.T
            vset_ld_dict[chr] = vset_ld
        return vset_ld_dict

    def _get_vset_half_covar_proj(self):
        covar_U, _, covar_Vt = np.linalg.svd(self.covar, full_matrices=False)
        half_covar_proj = np.dot(covar_U, covar_Vt).astype(np.float32)
        vset_half_covar_proj_dict = dict()
        for chr, vset in self.sparse_genotype_dict.items():
            vset_half_covar_proj = vset @ half_covar_proj
            vset_half_covar_proj_dict[chr] = vset_half_covar_proj
        return vset_half_covar_proj_dict

    def _get_var(self):
        var_dict = dict()
        for chr, resid_ldr in self.resid_ldr_dict.items():
            inner_ldr = np.dot(resid_ldr.T, resid_ldr).astype(np.float32)
            var = np.sum(np.dot(self.bases, inner_ldr) * self.bases, axis=1)
            var /= self.n_subs - self.n_covars # (N, )
            var_dict[chr] = var
        return var_dict

    def _get_cov_mat(self):
        """
        Compute Z'(I-M)Z for all variant sets
        
        """
        chr_cov_mat_dict = dict()
        for chr, gene_numeric_idxs in self.chr_gene_numeric_idxs.items():
            cov_mat_dict = dict()
            for bin, gene_numeric_idx in gene_numeric_idxs.items():
                cov_mat_list = list()
                for numeric_idx in gene_numeric_idx:
                    vset_half_covar_proj = self.vset_half_covar_proj_dict[chr][numeric_idx]
                    vset_ld = self.vset_ld_dict[chr][numeric_idx][:, numeric_idx]
                    cov_mat = np.array((vset_ld - vset_half_covar_proj @ vset_half_covar_proj.T))
                    cov_mat_list.append(cov_mat)
                cov_mat_dict[bin] = cov_mat_list
            chr_cov_mat_dict[chr] = cov_mat_dict
        return chr_cov_mat_dict

    def _get_vset_set(self):
        vset_set_dict = dict()
        for chr, var in self.var_dict.items():
            vset_set = VariantSetTest(self.bases, var, self.perm, np.arange(self.n_voxels))
            vset_set_dict[chr] = vset_set
        return vset_set_dict

    def _compute_sumstats(self):
        half_ldr_score_dict = dict()
        for chr, resid_ldr in self.resid_ldr_dict.items():
            half_ldr_score = self.sparse_genotype_dict[chr] @ resid_ldr
            half_ldr_score_dict[chr] = half_ldr_score
        return half_ldr_score_dict
    
    @log_execution_time
    def get_image_specific(self, bases, perm, resid_ldr_dict):
        self.bases = bases.astype(np.float32)
        self.n_voxels = self.bases.shape[0]
        self.perm = perm
        self.resid_ldr_dict = resid_ldr_dict
        self.var_dict = self._get_var()
        self.vset_set_dict = self._get_vset_set()
        self.half_ldr_score_dict = self._compute_sumstats()

    def _variant_set_test(self):
        """
        A wrapper function of variant set test for multiple sets
        
        """
        chr_sig_count_dict = dict()
        for chr, gene_numeric_idxs in self.chr_gene_numeric_idxs.items():
            sig_count_dict = dict()
            for bin, gene_numeric_idx in gene_numeric_idxs.items(): 
                sig_count_list = list()
                for gene_id, numeric_idx in enumerate(gene_numeric_idx):
                    half_ldr_score = self.half_ldr_score_dict[chr][numeric_idx]
                    cov_mat = self.chr_cov_mat_dict[chr][bin][gene_id]
                    maf = self.maf_dict[chr][numeric_idx]
                    cmac = int(np.sum(self.mac_dict[chr][numeric_idx]))
                    vset_test = self.vset_set_dict[chr]
                    sig_count = self._variant_set_test_(
                        half_ldr_score, cov_mat, maf, cmac, vset_test
                    )
                    sig_count_list.append(sig_count)
                sig_count_dict[bin] = sig_count_list
            chr_sig_count_dict[chr] = sig_count_dict

        return chr_sig_count_dict

    def _variant_set_test_(self, half_ldr_score, cov_mat, maf, cmac, vset_test):
        """
        Testing a single variant set
        
        """
        vset_test.input_vset(half_ldr_score, cov_mat, maf, cmac, None, None)
        pvalues, _ = vset_test.do_inference_tests(['staar'], None, False)
        pvalues = pvalues.iloc[:, 0]
        sig_count = np.nansum(pvalues < self.sig_thresh)

        return sig_count
    
    @log_execution_time
    def run(self, sample_id):
        """
        The main function for doing simulation

        """
        chr_sig_count_dict = self._variant_set_test()
        bin_sig_count_dict = {cmac_bin: list() for cmac_bin in self.all_bins}
        for _, sig_count_dict in chr_sig_count_dict.items():
            for cmac_bin, sig_count_list in sig_count_dict.items():
                bin_sig_count_dict[cmac_bin].extend(sig_count_list)

        bin_sig_count_dict_ = dict()
        for cmac_bin, sig_count_list in bin_sig_count_dict.items():
            cmac_bin_str = "_".join([str(x) for x in cmac_bin])
            bin_sig_count_dict_[cmac_bin_str] = np.mean(sig_count_list) / self.n_voxels

        return pd.DataFrame(bin_sig_count_dict_, index=[sample_id])


@log_execution_time
def creating_mask_null(mac_dict, cmac_bins_count=50000):
    """
    Creating masks for type I error evaluation

    Parameters:
    ------------
    mac_dict: a dict of mac per chr
    cmac_bins_count: #genes per cmac bin

    Returns:
    ---------
    chr_gene_numeric_idxs: a dict of dict of gene idxs
    
    """
    chr_gene_numeric_idxs = dict()
    cmac_bins = [(2,2), (3,3), (4,4), (5,5), (6,7), (8,9),
                 (10,11), (12,14), (15,20), (21,30), (31,60), 
                 (61,100), (101,500), (501,1000)]
    n_variants_list = np.array([len(mac) for _, mac in mac_dict.items()])
    chr_list = list(mac_dict.keys())
    n_genes_chr_list = (n_variants_list / np.sum(n_variants_list) * cmac_bins_count).astype(int)

    for i, chr in enumerate(chr_list):
        mac = mac_dict[chr]
        n_variants = n_variants_list[i]
        n_genes = n_genes_chr_list[i]
        variant_idxs = np.arange(n_variants)
        gene_numeric_idxs = dict() 
        for bin in cmac_bins:
            output = list()
            window_range = (max(2, int(bin[0]*0.1)), bin[1] + 1)
            while True:
                permuted_variant_idxs = variant_idxs[np.random.permutation(n_variants)]
                start = 0
                window_size = np.random.randint(*window_range)
                skip_size = int(window_size * 0.8) + 1
                while start + window_size < n_variants:
                    end = start + window_size
                    selected_variants = permuted_variant_idxs[start: end]
                    cmac = np.sum(mac[selected_variants]) 
                    if bin[0] <= cmac <= bin[1]:
                        output.append(selected_variants)
                        if len(output) >= n_genes:
                            break
                    start += skip_size
                if len(output) >= n_genes:
                    break
            gene_numeric_idxs[bin] = output
        chr_gene_numeric_idxs[chr] = gene_numeric_idxs

    return chr_gene_numeric_idxs


@log_execution_time
def creating_mask_causal(mac_dict, causal_idx_dict, cmac_bins_count=50000):
    """
    Creating masks for power evaluation

    Parameters:
    ------------
    mac_dict: a dict of mac per chr
    causal_idx_dict: a dict of causal idxs per chr
    cmac_bins_count: #genes per cmac bin

    Returns:
    ---------
    gene_numeric_idxs: a list of list of variant idxs for each gene
    
    """
    chr_gene_numeric_idxs = dict()
    cmac_bins = [(2,2), (3,3), (4,4), (5,5), (6,7), (8,9),
                 (10,11), (12,14), (15,20), (21,30), (31,60), 
                 (61,100), (101,500), (501,1000)]
    n_variants_list = np.array([len(mac) for _, mac in mac_dict.items()])
    chr_list = list(mac_dict.keys())
    n_genes_chr_list = (n_variants_list / np.sum(n_variants_list) * cmac_bins_count).astype(int)

    for i, chr in enumerate(chr_list):
        mac = mac_dict[chr]
        n_variants = n_variants_list[i]
        n_genes = n_genes_chr_list[i]
        causal_idxs = causal_idx_dict[chr]

        ## remove causal variants
        variant_idxs = np.setdiff1d(np.arange(n_variants), causal_idxs)
        n_variants = len(variant_idxs)

        ## get a dict of mac-position
        mac_positions = defaultdict(list)
        for mac_i, x in enumerate(mac):
            mac_positions[x].append(mac_i)

        ## get a dict of causal mac-position
        causal_mac_positions = defaultdict(list)
        for idx in causal_idxs:
            causal_mac_positions[mac[idx]].append(idx)

        gene_numeric_idxs = dict() 
        for bin in cmac_bins:
            output = list()
            while True:
                # upper_bound = bin[0] if bin[0] < 50 else int(bin[0] * 0.8)
                upper_bound = max(int(((bin[0] + bin[1]) // 2 * 0.5)), 2)
                causal_variants_cmac = np.random.randint(1, upper_bound)
                causal_variants = select_variants_for_cmac(causal_mac_positions, causal_variants_cmac)
                n_added = 0
                while n_added < 10:
                    non_causal_variants_cmac = np.random.randint(
                        max(bin[0]-causal_variants_cmac, 1), bin[1]-causal_variants_cmac+1
                    )
                    non_causal_variants = select_variants_for_cmac(mac_positions, non_causal_variants_cmac)
                    selected_variants = np.concatenate([non_causal_variants, causal_variants])
                    # assert bin[0] <= np.sum(mac[selected_variants]) <= bin[1]
                    output.append(selected_variants)
                    n_added += 1
                    if len(output) >= n_genes:
                        break
                if len(output) >= n_genes:
                    break
            gene_numeric_idxs[bin] = output
        chr_gene_numeric_idxs[chr] = gene_numeric_idxs

    return chr_gene_numeric_idxs


def select_variants_for_cmac(mac_positions, cmac):
    """
    Randomly select variants for the target cmac
    
    """
    macs = np.array(list(mac_positions.keys()))
    macs = macs[macs <= cmac]
    combo = list()
    current_cmac = 0
    while current_cmac < cmac:
        mac_ = np.random.choice(macs)
        if current_cmac + mac_ <= cmac:
            combo.append(mac_)
            current_cmac += mac_
            macs = macs[macs <= cmac - current_cmac]

    selected_variants = list()
    for mac in combo:
        n_variants = len(mac_positions[mac])
        x = np.random.uniform(0, 1)
        i = int(n_variants * x)
        j = min(i + 20, n_variants)
        selected_variants.append(np.random.choice(mac_positions[mac][i:j]))

    return np.array(selected_variants)


def check_input(args):
    if args.sparse_genotype is None:
        raise ValueError("--sparse-genotype is required")
    if args.null_model is None:
        raise ValueError("--null-model is required")
    if args.perm is None:
        raise ValueError("--perm is required")
    if args.causal_idx is not None:
        chr_list = list(range(1, 23, 2))
    else:
        chr_list = list(range(2, 23, 2))
    
    return chr_list


def run(args, log):
    chr_list = check_input(args)
    try:
        # reading data and selecting LDRs
        log.info(f"Read null model from {args.null_model}")
        null_model = NullModel(args.null_model)
        null_model.select_ldrs(args.n_ldrs)

        # reading sparse genotype data
        sparse_genotype_dict = dict()
        mac_dict = dict()
        maf_dict = dict()
        n_variants = 0

        for chr in chr_list:
            sparse_genotype_file = args.sparse_genotype.replace('@', str(chr))
            sparse_genotype = load_npz(sparse_genotype_file)
            mac = np.squeeze(np.array(sparse_genotype.sum(axis=1)))
            maf = mac / sparse_genotype.shape[1] / 2
            n_variants += sparse_genotype.shape[0]
            sparse_genotype_dict[chr] = sparse_genotype
            mac_dict[chr] = mac
            maf_dict[chr] = maf
        n_subs = sparse_genotype_dict[chr].shape[1]

        log.info(f"Read sparse genotype data from {args.sparse_genotype}")
        log.info(f"{n_subs} subjects and {n_variants} variants.")

        if args.causal_idx is not None:
            causal_idx_dict = dict()
            for chr in chr_list:
                causal_idx_file = args.causal_idx.replace('@', str(chr))
                causal_idx_dict[chr] = np.loadtxt(causal_idx_file).astype(int)
        else:
            causal_idx_dict = None

        # reading loco preds
        if args.loco_preds is not None:
            log.info(f"Read LOCO predictions from {args.loco_preds}")
            loco_preds = LOCOpreds(args.loco_preds)
            if args.n_ldrs is not None:
                loco_preds.select_ldrs((0, args.n_ldrs))
            if loco_preds.ldr_col[1] - loco_preds.ldr_col[0] != null_model.n_ldrs:
                raise ValueError(
                    (
                        "inconsistent dimension in LDRs and LDR LOCO predictions. "
                        "Try to use --n-ldrs"
                    )
                )
        else:
            loco_preds = None

        log.info(
            (
                f"{null_model.covar.shape[1]} fixed effects in the covariates "
                "(including the intercept) after removing redundant effects.\n"
            )
        )
        
        # reading permutation
        log.info(f"Read permutation from {args.perm}")
        perm = PermDistribution(args.perm)

        # split genotype into regions
        if causal_idx_dict is None:
            chr_gene_numeric_idxs = creating_mask_null(mac_dict)
        else:
            chr_gene_numeric_idxs = creating_mask_causal(mac_dict, causal_idx_dict)

        # adjust for sample relatedness
        resid_ldr_dict = dict()
        for chr in chr_list:
            if args.loco_preds is not None:
                resid_ldr_dict[chr] = null_model.resid_ldr - loco_preds.data_reader(chr)
            else:
                resid_ldr_dict[chr] = null_model.resid_ldr

        # simulation
        rv_simulation = RVsimulation(
            null_model.covar, 
            sparse_genotype_dict, 
            chr_gene_numeric_idxs, 
            maf_dict, 
            mac_dict,
            2.5e-6,
        )

        rv_simulation.get_image_specific(null_model.bases, perm, resid_ldr_dict)
        bin_sig_count = rv_simulation.run(0)

        out_path = f"{args.out}.txt"
        # bin_sig_count.to_csv(out_path, sep='\t', index=None)
        bin_sig_count = bin_sig_count.to_csv(sep='\t', index=None, header=None)
        current_time = str(datetime.now())
        with open(out_path, 'a') as file:
            file.write(f"{current_time}\t{bin_sig_count}")
            
        log.info(f"\nSave results to {args.out}.txt")

    finally:
        if args.loco_preds is not None and loco_preds in locals():
            loco_preds.close()


parser = argparse.ArgumentParser()
parser.add_argument('--null-model')
parser.add_argument('--sparse-genotype')
parser.add_argument('--loco-preds')
parser.add_argument('--out')
parser.add_argument('--n-ldrs', type=int)
parser.add_argument('--causal-idx')
parser.add_argument('--perm')


if __name__ == '__main__':
    args = parser.parse_args()

    if args.out is None:
        args.out = "heig"

    logpath = os.path.join(f"{args.out}.log")
    log = GetLogger(logpath)

    start_time = time.time()
    try:
        defaults = vars(parser.parse_args(""))
        opts = vars(args)
        non_defaults = [x for x in opts.keys() if opts[x] != defaults[x]]
        header = "run_simulation.py \\\n"
        options = [
            "--" + x.replace("_", "-") + " " + str(opts[x]) + " \\"
            for x in non_defaults
        ]
        header += "\n".join(options).replace(" True", "").replace(" False", "")
        header = header + "\n"
        log.info(header)
        run(args, log)
    except Exception:
        log.info(traceback.format_exc())
        raise
    finally:
        log.info(f"\nAnalysis finished at {time.ctime()}")
        time_elapsed = round(time.time() - start_time, 2)
        log.info(f"Total time elapsed: {sec_to_str(time_elapsed)}")