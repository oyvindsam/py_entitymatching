# coding=utf-8
import logging
import re
import string
from collections import OrderedDict, Counter

import pandas as pd
import pyprind
import six

import magellan.catalog.catalog_manager as cm
from magellan.blocker.blocker import Blocker

# from magellan.externals.py_stringmatching.tokenizers import qgram
from py_stringmatching.tokenizer.whitespace_tokenizer import WhitespaceTokenizer
from py_stringmatching.tokenizer.qgram_tokenizer import QgramTokenizer

from magellan.utils.catalog_helper import log_info, get_name_for_key, \
    add_key_column

from magellan.externals.py_stringsimjoin.filter.overlap_filter import OverlapFilter

from magellan.utils.generic_helper import remove_non_ascii, rem_nan

from functools import partial

logger = logging.getLogger(__name__)


class OverlapBlocker(Blocker):
    """Blocks two tables, a candset, or a pair of tuples based on the overlap
       of token sets of attribute values.
    """

    def __init__(self):
        self.stop_words = ['a', 'an', 'and', 'are', 'as', 'at',
                           'be', 'by', 'for', 'from',
                           'has', 'he', 'in', 'is', 'it',
                           'its', 'on', 'that', 'the', 'to',
                           'was', 'were', 'will', 'with']
        self.regex_punctuation = re.compile(
            '[%s]' % re.escape(string.punctuation))
        super(OverlapBlocker, self).__init__()

    def block_tables(self, ltable, rtable, l_overlap_attr, r_overlap_attr,
                     rem_stop_words=False, q_val=None, word_level=True,
                     overlap_size=1,
                     l_output_attrs=None, r_output_attrs=None,
                     l_output_prefix='ltable_', r_output_prefix='rtable_',
                     verbose=False, show_progress=True, n_jobs=1):
        """Blocks two tables based on the overlap of token sets of attribute
           values.

        Finds tuple pairs from left and right tables such that the overlap
        between (a) the set of tokens obtained by tokenizing the value of
        attribute l_overlap_attr of a tuple from the left table, and (b) the
        set of tokens obtained by tokenizing the value of attribute
        r_overlap_attr of a tuple from the right table, is above a certain
        threshold.

        Args:
            ltable (Dataframe): left input table.

            rtable (Dataframe): right input table.

            l_overlap_attr (str): overlap attribute in left table.

            r_overlap_attr (str): overlap attribute in right table. 

            rem_stop_words (boolean): flag to indicate whether stop words
                                      (e.g., a, an, the) should be removed
                                      from the token sets of the overlap
                                      attribute values (defaults to False).

            q_val (int): value of q to use if the overlap attributes values
                         are to be tokenized as qgrams (defaults to None).
 
            word_level (boolean): flag to indicate whether the overlap
                                  attributes should be tokenized as words
                                  (i.e, using whitespace as delimiter)
                                  (defaults to True).

            overlap_size (int): minimum number of tokens that must overlap
                                (defaults to 1).

            l_output_attrs (list): list of attribute names from the left
                                   table to be included in the
                                   output candidate set (defaults to None).

            r_output_attrs (list): list of attribute names from the right
                                   table to be included in the
                                   output candidate set (defaults to None).

            l_output_prefix (str): prefix to be used for the attribute names
                                   coming from the left table in the output
                                   candidate set (defaults to 'ltable\_').

            r_output_prefix (str): prefix to be used for the attribute names
                                   coming from the right table in the output
                                   candidate set (defaults to 'rtable\_').

            verbose (boolean): flag to indicate whether logging should be done
                               (defaults to False).

            show_progress (boolean): flag to indicate whether progress should
                                     be displayed to the user (defaults to True).

            n_jobs (int): number of parallel jobs to be used for computation
                          (defaults to 1).
                          If -1 all CPUs are used. If 0 or 1, no parallel computation
                          is used at all, which is useful for debugging.
                          For n_jobs below -1, (n_cpus + 1 + n_jobs) are used.
                          Thus, for n_jobs = -2, all CPUS but one are used.
                          If (n_cpus + 1 + n_jobs) is less than 1, then n_jobs is
                          set to 1, which means no parallel computation at all.

        Returns:
            A candidate set of tuple pairs that survived blocking (DataFrame).
        """

        # validate data types of standard input parameters
        self.validate_types_params_tables(ltable, rtable,
			    l_output_attrs, r_output_attrs, l_output_prefix,
			    r_output_prefix, verbose, show_progress, n_jobs)

        # validate data types of input parameters specific to overlap blocker
        self.validate_types_other_params(l_overlap_attr, r_overlap_attr,
                                         rem_stop_words, q_val,
                                         word_level, overlap_size)
 
        # validate overlap attributes
        self.validate_overlap_attrs(ltable, rtable, l_overlap_attr,
                                    r_overlap_attr)

        # validate output attributes
        self.validate_output_attrs(ltable, rtable, l_output_attrs,
                                   r_output_attrs)

        # get and validate required metadata
        log_info(logger, 'Required metadata: ltable key, rtable key', verbose)

        # # get metadata
        l_key, r_key = cm.get_keys_for_ltable_rtable(ltable, rtable, logger,
                                                     verbose)

        # # validate metadata
        cm._validate_metadata_for_table(ltable, l_key, 'ltable', logger, verbose)
        cm._validate_metadata_for_table(rtable, r_key, 'rtable', logger, verbose)

        # validate word_level and q_val
        self.validate_word_level_qval(word_level, q_val)  

        # do blocking

        # # remove nans: should be modified based on missing data policy
        l_df = rem_nan(ltable, l_overlap_attr)
        r_df = rem_nan(rtable, r_overlap_attr)

        # # do projection before merge
        l_proj_attrs = self.get_attrs_to_project(l_key, l_overlap_attr, l_output_attrs)
        l_df = l_df[l_proj_attrs]
        r_proj_attrs = self.get_attrs_to_project(r_key, r_overlap_attr, r_output_attrs)
        r_df = r_df[r_proj_attrs]

        # # case the column to string if required.
        if l_df.dtypes[l_overlap_attr] != object:
            logger.warning('Left overlap attribute is not of type string; converting to string temporarily')
            l_df[l_overlap_attr] = l_df[l_overlap_attr].astype(str)

        if r_df.dtypes[r_overlap_attr] != object:
            logger.warning('Right overlap attribute is not of type string; converting to string temporarily')
            r_df[r_overlap_attr] = r_df[r_overlap_attr].astype(str)

        # # cleanup the tables from non-ascii characters, punctuations, and stop words
        self.cleanup_table(l_df, l_overlap_attr, rem_stop_words)
        self.cleanup_table(r_df, r_overlap_attr, rem_stop_words)

        # # determine which tokenizer to use
        if word_level == True:
            # # # create a whitespace tokenizer
            tokenizer = WhitespaceTokenizer(return_set=True)
        else:
            # # # create a qgram tokenizer 
            tokenizer = QgramTokenizer(qval=q_val, return_set=True)

        # # create a overlap filter for similarity join
        overlap_filter = OverlapFilter(tokenizer, overlap_size)
        
        # # determine number of processes to launch parallely
        n_procs = self.get_num_procs(n_jobs, len(r_df))
        if n_procs < 1:
            n_procs = 1 

        # # perform overlap similarity join
        candset = overlap_filter.filter_tables(l_df, r_df, l_key, r_key,
                                               l_overlap_attr, r_overlap_attr,
                                               l_output_attrs, r_output_attrs,
                                               l_output_prefix, r_output_prefix,
                                               out_sim_score=False,
                                               n_jobs=n_procs)
        print('candset cols:', candset.columns)

        # # retain only the required attributes in the output candidate set 
        retain_cols = self.get_attrs_to_retain(l_key, r_key, l_output_attrs, r_output_attrs,
                                               l_output_prefix, r_output_prefix)
        print('retain_cols:', retain_cols)
        candset = candset[retain_cols]

        # update metadata in the catalog
        key = get_name_for_key(candset.columns)
        candset = add_key_column(candset, key)
        cm.set_candset_properties(candset, key, l_output_prefix + l_key,
                                  r_output_prefix + r_key, ltable, rtable)

        # return the candidate set
        return candset

    def block_candset(self, candset, l_overlap_attr, r_overlap_attr,
                      rem_stop_words=False, q_val=None, word_level=True, overlap_size=1,
                      verbose=False, show_progress=True, n_jobs=1):
        """Blocks an input candidate set of tuple pairs based on the overlap
           of token sets of attribute values.

        Finds tuple pairs from an input candidate set of tuple pairs such that
        the overlap between (a) the set of tokens obtained by tokenizing the
        value of attribute l_overlap_attr of the left tuple in a tuple pair,
        and (b) the set of tokens obtained by tokenizing the value of
        attribute r_overlap_attr of the right tuple in the tuple pair,
        is above a certain threshold.

        Args:
            candset (DataFrame): input candidate set of tuple pairs.

            l_overlap_attr (str): overlap attribute in left table.

            r_overlap_attr (str): overlap attribute in right table. 

            rem_stop_words (boolean): flag to indicate whether stop words
                                      (e.g., a, an, the) should be removed
                                      from the token sets of the overlap
                                      attribute values (defaults to False).

            q_val (int): value of q to use if the overlap attributes values
                         are to be tokenized as qgrams (defaults to None).
 
            word_level (boolean): flag to indicate whether the overlap
                                  attributes should be tokenized as words
                                  (i.e, using whitespace as delimiter)
                                  (defaults to True).

            overlap_size (int): minimum number of tokens that must overlap
                                (defaults to 1).

            verbose (boolean): flag to indicate whether logging should be done
                               (defaults to False).

            show_progress (boolean): flag to indicate whether progress should
                                     be displayed to the user (defaults to True).

            n_jobs (int): number of parallel jobs to be used for computation
                          (defaults to 1).
                          If -1 all CPUs are used. If 0 or 1, no parallel computation
                          is used at all, which is useful for debugging.
                          For n_jobs below -1, (n_cpus + 1 + n_jobs) are used.
                          Thus, for n_jobs = -2, all CPUS but one are used.
                          If (n_cpus + 1 + n_jobs) is less than 1, then n_jobs is
                          set to 1, which means no parallel computation at all.

        Returns:
            A candidate set of tuple pairs that survived blocking (DataFrame).
        """

        # validate data types of standard input parameters
        self.validate_types_params_candset(candset, verbose, show_progress, n_jobs)

        # validate data types of input parameters specific to overlap blocker
        self.validate_types_other_params(l_overlap_attr, r_overlap_attr,
                                         rem_stop_words, q_val,
                                         word_level, overlap_size)

        # get and validate metadata
        log_info(logger,
                 'Required metadata: cand.set key, fk ltable, fk rtable, '
                 'ltable, rtable, ltable key, rtable key', verbose)

        # # get metadata
        key, fk_ltable, fk_rtable, ltable, rtable, l_key, r_key = cm.get_metadata_for_candset(
            candset, logger, verbose)

        # # validate metadata
        cm._validate_metadata_for_candset(candset, key, fk_ltable, fk_rtable,
                                          ltable, rtable, l_key, r_key,
                                          logger, verbose)

        # validate overlap attrs
        self.validate_overlap_attrs(ltable, rtable, l_overlap_attr,
                                    r_overlap_attr)

        # validate word_level and q_val
        self.validate_word_level_qval(word_level, q_val)  

        # do blocking

        # # remove nans: should be modified based on missing data policy
        l_df = rem_nan(ltable, l_overlap_attr)
        r_df = rem_nan(rtable, r_overlap_attr)

        # # do projection before merge
        l_df = l_df[[l_key, l_overlap_attr]]
        r_df = r_df[[r_key, r_overlap_attr]]

        # # case the column to string if required.
        if l_df.dtypes[l_overlap_attr] != object:
            logger.warning('Left overlap attribute is not of type string; coverting to string temporarily')
            l_df[l_overlap_attr] = l_df[l_overlap_attr].astype(str)

        if r_df.dtypes[r_overlap_attr] != object:
            logger.warning('Right overlap attribute is not of type string; coverting to string temporarily')
            r_df[r_overlap_attr] = r_df[r_overlap_attr].astype(str)

        # # cleanup the tables from non-ascii characters, punctuations, and stop words
        self.cleanup_table(l_df, l_overlap_attr, rem_stop_words)
        self.cleanup_table(r_df, r_overlap_attr, rem_stop_words)

        # # determine which tokenizer to use
        if word_level == True:
            # # # create a whitespace tokenizer
            tokenizer = WhitespaceTokenizer(return_set=True)
        else:
            # # # create a qgram tokenizer
            tokenizer = QgramTokenizer(qval=q_val, return_set=True)
       
        # # create a filter for overlap similarity join
        overlap_filter = OverlapFilter(tokenizer, overlap_size)

        # # determine number of processes to launch parallely
        n_procs = self.get_num_procs(n_jobs, len(candset)) 
        if n_procs < 1:
            n_procs = 1

        # # perform overlap similarity filtering of the candset
        out_table = overlap_filter.filter_candset(candset, fk_ltable, fk_rtable,
                                                  l_df, r_df, l_key, r_key,
                                                  l_overlap_attr, r_overlap_attr,
                                                  n_jobs=n_procs)
        # update catalog
        cm.set_candset_properties(out_table, key, fk_ltable, fk_rtable, ltable, rtable)

        # return candidate set
        return out_table

    def block_tuples(self, ltuple, rtuple, l_overlap_attr, r_overlap_attr,
                     rem_stop_words=False, q_val=None, word_level=True,
                     overlap_size=1):
        """Blocks a tuple pair based on the overlap of token sets of attribute
           values.
        
        Args:
            ltuple (Series): input left tuple.

            rtuple (Series): input right tuple.
            
            l_overlap_attr (str): overlap attribute in left tuple.

            r_overlap_attr (str): overlap attribute in right tuple.

            rem_stop_words (boolean): flag to indicate whether stop words
                                      (e.g., a, an, the) should be removed
                                      from the token sets of the overlap
                                      attribute values (defaults to False).

            q_val (int): value of q to use if the overlap attributes values
                         are to be tokenized as qgrams (defaults to None).
 
            word_level (boolean): flag to indicate whether the overlap
                                  attributes should be tokenized as words
                                  (i.e, using whitespace as delimiter)
                                  (defaults to True).

            overlap_size (int): minimum number of tokens that must overlap
                                (defaults to 1).

        Returns:
            A status indicating if the tuple pair is blocked (boolean).
        """
        
        # validate data types of input parameters specific to overlap blocker
        self.validate_types_other_params(l_overlap_attr, r_overlap_attr,
                                         rem_stop_words, q_val,
                                         word_level, overlap_size)
 
        # validate word_level and q_val
        self.validate_word_level_qval(word_level, q_val)  

        # determine which tokenizer to use
        if word_level == True:
            # # create a whitespace tokenizer
            tokenizer = WhitespaceTokenizer(return_set=True)
        else:
            # # create a qgram tokenizer 
            tokenizer = QgramTokenizer(qval=q_val, return_set=True)

        # create a filter for overlap similarity 
        overlap_filter = OverlapFilter(tokenizer, overlap_size)

        return overlap_filter.filter_pair(ltuple[l_overlap_attr], rtuple[r_overlap_attr])
        

    # helper functions

    # validate the data types of input parameters specific to overlap blocker
    def validate_types_other_params(self, l_overlap_attr, r_overlap_attr,
                                    rem_stop_words, q_val,
                                    word_level, overlap_size):
        if not isinstance(l_overlap_attr, six.string_types):
            logger.error('Overlap attribute name of left table is not of type string')
            raise AssertionError('Overlap attribute name of left table is not of type string')
        if not isinstance(r_overlap_attr, six.string_types):
            logger.error('Overlap attribute name of right table is not of type string')
            raise AssertionError('Overlap attribute name of right table is not of type string')
        if not isinstance(rem_stop_words, bool):
            logger.error('Parameter rem_stop_words is not of type bool')
            raise AssertionError('Parameter rem_stop_words is not of type bool')
        if q_val != None and not isinstance(q_val, int):
            logger.error('Parameter q_val is not of type int')
            raise AssertionError('Parameter q_val is not of type int')
        if not isinstance(word_level, bool):
            logger.error('Parameter word_level is not of type bool')
            raise AssertionError('Parameter word_level is not of type bool')
        if not isinstance(overlap_size, int):
            logger.error('Parameter overlap_size is not of type int')
            raise AssertionError('Parameter overlap_size is not of type int')

    # validate the overlap attrs
    def validate_overlap_attrs(self, ltable, rtable, l_overlap_attr, r_overlap_attr):
        if not isinstance(l_overlap_attr, list):
            l_overlap_attr = [l_overlap_attr]
        assert set(l_overlap_attr).issubset(
            ltable.columns) is True, 'Left block attribute is not in the left table'

        if not isinstance(r_overlap_attr, list):
            r_overlap_attr = [r_overlap_attr]
        assert set(r_overlap_attr).issubset(
            rtable.columns) is True, 'Right block attribute is not in the right table'

    # validate word_level and q_val
    def validate_word_level_qval(self, word_level, q_val):
        if word_level == True and q_val != None:
            raise SyntaxError('Parameters word_level and q_val cannot be set together; Note that word_level is '
                              'set to True by default, so explicity set word_level=false to use qgram with the '
                              'specified q_val')

        if word_level == False and q_val == None:
            raise SyntaxError('Parameters word_level and q_val cannot be unset together; Note that q_val is '
                              'set to None by default, so if you want to use qgram then '
                              'explictiy set word_level=False and specify the q_val')
    
    # cleanup a table from non-ascii characters, punctuations and stop words
    def cleanup_table(self, table, overlap_attr, rem_stop_words):

        # get overlap_attr column
        attr_col_values = table[overlap_attr]

        # remove non-ascii chars
        attr_col_values = [remove_non_ascii(val) for val in attr_col_values]

        # remove special characters
        attr_col_values = [self.rem_punctuations(val).lower() for val in
                           attr_col_values]

        # chop the attribute values
        col_values_chopped = [val.split() for val in attr_col_values]

        # convert the chopped values into a set
        col_values_chopped = [list(set(val)) for val in col_values_chopped]

        # remove stop words
        if rem_stop_words == True:
            col_values_chopped = [self.rem_stopwords(val) for val in
                                  col_values_chopped]

        values = [' '.join(val) for val in col_values_chopped]

        table[overlap_attr] = values

    def rem_punctuations(self, s):
        return self.regex_punctuation.sub('', s)

    def rem_stopwords(self, lst):
        return [t for t in lst if t not in self.stop_words]
