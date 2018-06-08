# -*- coding: utf-8 -*-
#
# Author: Taylor G Smith <taylor.smith@alkaline-ml.com>
#
# Bin your continuous features.

from __future__ import absolute_import

from sklearn.externals import six
from sklearn.utils.validation import check_is_fitted

import numpy as np

from ..base import BasePDTransformer
from ..utils.iterables import is_iterable, chunk
from ..utils.validation import check_dataframe, validate_test_set_columns

# Cython import
from ._binning import entropy_bin_bounds

__all__ = [
    'BinningTransformer'
]


def _validate_n_bins(x, n):
    # get unique values
    unique, cts = np.unique(x, return_counts=True)
    if unique.shape[0] < n:
        raise ValueError("Fewer unique values than bins!")
    return unique, cts


def _entropy(x, n):
    x = np.asarray(x, dtype=np.float64)  # needs to be double for C code
    unique, cts = _validate_n_bins(x, n)
    return _Bins(entropy_bin_bounds(x, unique, cts.astype(np.float32)))


def _uniform(x, n):
    # get unique and cut it at the uniform points
    unique, _ = _validate_n_bins(x, n)
    chunks = list(chunk(unique, n))

    # So now our chunks may resemble:
    # >>> list(chunk(np.arange(10), 4))
    # [array([0, 1, 2]), array([3, 4, 5]), array([6, 7]), array([8, 9])]
    # Transform them to bins
    return _Bins(chunks)


_STRATEGIES = {"entropy": _entropy,
               "uniform": _uniform}


class _Bins(object):
    """Binning class that keeps track of upper and lower bounds of bins.
    The algorithm for assigning bins to a test vector is as follows:

        1. Initialize all bins as the highest bin
        2. For each lower bound in bin levels, determine which values in ``x``
           are >= to the bound. Invert the mask and decrement those bins (in
           other words, decrement the indices where the value is < the lower
           bound for the bin in question).
        3. Continue until there is no mask to invert (lowest bin).
    """
    def __init__(self, chunks):
        # chunks is a list of bin arrays
        self.n_bins = len(chunks)

        # create the repr for each bin and create the mins/maxes arrays
        upper_bounds = []
        lower_bounds = []
        reprs = []
        for i, (this_chunk, next_chunk) in \
                enumerate(zip(chunks[:-1], chunks[1:])):

            # If it's the first one, it's just less than
            # the next chunk's min.
            upper_bound = next_chunk[0]
            if i == 0:
                lower_bound = -np.inf
                rep = "(-Inf, %.2f]" % upper_bound

            # Otherwise we know it's a middle one (not the last since we
            # lagged with the zip function and handle that at the end)
            else:
                lower_bound = this_chunk[0]
                rep = "(%.2f, %.2f]" % (lower_bound, upper_bound)

            upper_bounds.append(upper_bound)
            lower_bounds.append(lower_bound)
            reprs.append(rep)

        # since we missed the last chunk due to the lag, get the last one
        lower_bounds.append(chunks[-1][0])
        upper_bounds.append(np.inf)
        reprs.append("(%.2f, Inf]" % lower_bounds[-1])

        # set the attributes
        self.upper_bounds = upper_bounds
        self.lower_bounds = lower_bounds
        self.reprs = reprs

    def assign(self, v, as_str):
        # given some vector of values, assign the appropriate bins. We can
        # do this in one pass, really. Just pass over one of the bounds arrays
        # and keep track of the level at which the elements in V are no longer
        # within the boundaries

        # Initialize by setting all to the highest bin
        bins = (np.ones(v.shape[0]) * (self.n_bins - 1)).astype(int)

        # now progress backwards
        for boundary in self.lower_bounds[::-1]:

            # figure out which are >= to the lower boundary. They should NOT
            # be changed. The ones that are FALSE, however, should be
            # decremented by 1. On the first pass, anything that actually
            # belongs in the top bin will not be adjusted, but everything
            # else will drop by one. Next, everything that is still below the
            # lower boundary will decrement again, etc., until the lowest bin
            # where the lower_bound is -np.inf. Since everything is >= that,
            # there will be no anti mask and nothing will change
            mask = v >= boundary
            anti_mask = ~mask  # type: np.ndarray

            if anti_mask.shape[0] > 0:
                bins[anti_mask] -= 1

        # now we have bin indices, get the reprs to return...
        if as_str:
            return np.array([self.reprs[i] for i in bins])
        # otherwise user just wants the bin level
        return bins


class BinningTransformer(BasePDTransformer):
    r"""Bin continuous variables.

    The BinningTransformer will create buckets for continuous variables,
    effectively transforming continuous features into categorical features.

    Pros of binning:

      * Particularly useful in the case of very skewed data where an
        algorithm may make assumptions on the underlying distribution of the
        variables
      * Quick and easy way to take curvature into account

    There are absolutely some negatives to binning:
    
      * You can tend to throw away information from continuous variables
      * You might end up fitting "wiggles" rather than a linear
        relationship itself
      * You use up a lot of degrees of freedom

    For a more exhaustive list of detrimental effects of binning, take a look
    at [1].

    Parameters
    ----------
    cols : array-like, shape=(n_features,)
        The names of the columns on which to apply the transformation.
        Unlike other BasePDTransformer instances, this is not optional,
        since binning the entire frame could prove extremely expensive
        if accidentally applied to continuous data.

    as_df : bool, optional (default=True)
        Whether to return a Pandas ``DataFrame`` in the ``transform``
        method. If False, will return a Numpy ``ndarray`` instead.
        Since most skoot transformers depend on explicitly-named
        ``DataFrame`` features, the ``as_df`` parameter is True by default.

    n_bins : int or iterable, optional (default=10)
        The number of bins into which to separate each specified feature.
        Default is 20, but can also be an iterable or dict of the same length
        as ``cols``, where positional integers indicate a different bin size
        for that feature.

    strategy : str or unicode, optional (default="uniform")
        The strategy for binning. Default is "uniform", which uniformly
        segments a feature. Alternatives include "entropy" which splits the
        feature based on the entropy score, much like splitting a decision
        tree.

    return_bin_label : bool, optional (default=True)
        Whether to return the string representation of the bin (i.e., "<25.2")
        rather than the bin level, an integer.

    overwrite : bool, optional (default=True)
        Whether to overwrite the original feature with the binned feature.
        Default is True so that the output names match the input names. If
        False, the output columns will be appended to the right side of
        the frame with "_binned" appended.

    Notes
    -----
    If a feature has fewer than ``n_bins`` unique values, it will raise a
    ValueError in the fit procedure.

    Examples
    --------
    Bin two features in iris:

    >>> from skoot.datasets import load_iris_df
    >>> iris = load_iris_df(include_tgt=False, names=['a', 'b', 'c', 'd'])
    >>> binner = BinningTransformer(cols=["a", "b"], strategy="uniform")
    >>> trans = binner.fit_transform(iris)
    >>> trans.head()
                  a             b    c    d
    0  (5.10, 5.50]  (3.40, 3.60]  1.4  0.2
    1  (4.70, 5.10]  (3.00, 3.20]  1.4  0.2
    2  (4.70, 5.10]  (3.20, 3.40]  1.3  0.2
    3  (-Inf, 4.70]  (3.00, 3.20]  1.5  0.2
    4  (4.70, 5.10]  (3.60, 3.80]  1.4  0.2
    >>> trans.dtypes
    a     object
    b     object
    c    float64
    d    float64
    dtype: object

    Attributes
    ----------
    bins_ : dict
        A dictionary mapping the column names to the corresponding bins,
        which are internal _Bin objects that store data on upper and lower
        bounds.

    fit_cols_ : list
        The list of column names on which the transformer was fit. This
        is used to validate the presence of the features in the test set
        during the ``transform`` stage.

    References
    ----------
    .. [1] "Problems Caused by Categorizing Continuous Variables"
           http://biostat.mc.vanderbilt.edu/wiki/Main/CatContinuous
    """
    def __init__(self, cols, as_df=True, n_bins=10, strategy="uniform",
                 return_bin_label=True, overwrite=True):

        super(BinningTransformer, self).__init__(
            cols=cols, as_df=as_df)

        self.n_bins = n_bins
        self.strategy = strategy
        self.return_bin_label = return_bin_label
        self.overwrite = overwrite

    def fit(self, X, y=None):
        """Fit the transformer.

        Parameters
        ----------
        X : pd.DataFrame, shape=(n_samples, n_features)
            The Pandas frame to fit. The frame will only
            be fit on the prescribed ``cols`` (see ``__init__``) or
            all of them if ``cols`` is None.

        y : array-like or None, shape=(n_samples,), optional (default=None)
            Pass-through for ``sklearn.pipeline.Pipeline``.
        """
        # validate the input, and get a copy of it
        X, cols = check_dataframe(X, cols=self.cols,
                                  assert_all_finite=True)

        # validate n_bins...
        n_bins = self.n_bins
        if is_iterable(n_bins):
            # first smoke test is easy -- if the length of the number of
            # bins does not match the number of columns prescribed, raise
            if len(n_bins) != len(cols):
                raise ValueError("dim mismatch between cols and n_bin")

            # next, we're concerned with whether the n_bins iterable is a dict
            # and if it is, we have to validate the keys are all there...
            if isinstance(n_bins, dict):

                # get sets of the columns and keys so we can easily compare
                scols = set(cols)
                skeys = set(n_bins.keys())

                # if there are extra keys (skeys - scols) or missing keys
                # from the prescribed columns (scols - skeys) we have to raise
                if scols - skeys or skeys - scols:
                    raise ValueError("When n_bins is provided as a dictionary "
                                     "its keys must match the provided cols.")

            # otherwise, what we ultimately want IS a dictionary
            else:
                n_bins = dict(zip(cols, n_bins))

        else:
            if not isinstance(n_bins, int):
                raise TypeError("n_bins must be an iterable or an int")

            # make it into a dictionary mapping cols to n_bins
            n_bins = {c: n_bins for c in cols}

        # now that we have a dictionary, we can assess the actual integer
        for _, v in six.iteritems(n_bins):
            if not (isinstance(v, int) and v > 1):
                raise ValueError("Each n_bin value must be an integer > 1")

        # get and validate the strategy
        strategy = self.strategy
        try:
            binner = _STRATEGIES[strategy]
        except KeyError:
            raise ValueError("strategy must be one of %r, but got %r"
                             % (str(list(_STRATEGIES.keys())), strategy))

        # compute the bins for each feature
        bins = {}
        for c, n in six.iteritems(n_bins):
            bins[c] = binner(X[c].values, n)

        # set the instance attribute
        self.bins_ = bins
        self.fit_cols_ = cols
        return self

    def transform(self, X):
        """Apply the transformation to a dataframe.

        This method will bin the continuous values in the test frame with the
        bins designated in the ``fit`` stage.

        Parameters
        ----------
        X : pd.DataFrame, shape=(n_samples, n_features)
            The Pandas frame to transform. The operation will
            be applied to a copy of the input data, and the result
            will be returned.

        Returns
        -------
        X : pd.DataFrame or np.ndarray, shape=(n_samples, n_features)
            The operation is applied to a copy of ``X``,
            and the result set is returned.
        """
        check_is_fitted(self, 'bins_')
        X, _ = check_dataframe(X, cols=self.cols)  # X is a copy now

        # validate that fit cols in test set
        cols = self.fit_cols_
        validate_test_set_columns(cols, X.columns)

        # the bins
        bins = self.bins_

        # now apply the binning. Rather that use iteritems, iterate the cols
        # themselves so we get the order prescribed by the user
        for col in cols:

            # get the bin
            bin_ = bins[col]  # O(1) lookup

            # get the feature from the frame as an array
            v = X[col].values  # type: np.ndarray
            binned = bin_.assign(v, self.return_bin_label)  # via _Bins class

            # if we overwrite, it's easy
            if self.overwrite:
                X[col] = binned
            # otherwise create a new feature
            else:
                X["%s_binned" % col] = binned

        return X if self.as_df else X.values
