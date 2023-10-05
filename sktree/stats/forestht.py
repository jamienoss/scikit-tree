from typing import Callable, Tuple, Union

import numpy as np
from joblib import Parallel, delayed
from numpy.typing import ArrayLike
from sklearn.base import MetaEstimatorMixin, clone, is_classifier
from sklearn.ensemble._forest import ForestClassifier as sklearnForestClassifier
from sklearn.ensemble._forest import ForestRegressor as sklearnForestRegressor
from sklearn.model_selection import train_test_split
from sklearn.utils.multiclass import type_of_target
from sklearn.utils.validation import _is_fitted, check_X_y

from sktree._lib.sklearn.ensemble._forest import (
    BaseForest,
    ForestClassifier,
    ForestRegressor,
    RandomForestClassifier,
    RandomForestRegressor,
    _get_n_samples_bootstrap,
    _parallel_build_trees,
)
from sktree.tree import DecisionTreeClassifier, DecisionTreeRegressor

from .utils import (
    METRIC_FUNCTIONS,
    POSITIVE_METRICS,
    POSTERIOR_FUNCTIONS,
    REGRESSOR_METRICS,
    _compute_null_distribution_coleman,
    _non_nan_samples,
)


def _parallel_build_trees_and_compute_posteriors(
    forest: BaseForest,
    idx: int,
    indices_train: ArrayLike,
    indices_test: ArrayLike,
    X: ArrayLike,
    y: ArrayLike,
    covariate_index,
    posterior_arr: ArrayLike,
    predict_posteriors: bool,
    permute_per_tree: bool,
    sample_weight: ArrayLike = None,
    class_weight=None,
    missing_values_in_feature_mask=None,
    classes=None,
):
    """Parallel function to build trees and compute posteriors.

    This inherently assumes that the caller function defines the indices
    for the training and testing data for each tree.
    """
    tree: Union[DecisionTreeClassifier, DecisionTreeRegressor] = forest.estimators_[idx]
    if permute_per_tree and covariate_index is not None:
        random_state = tree.random_state
    else:
        random_state = forest.random_state

    X_train = X[indices_train, :]
    y_train = y[indices_train, ...]
    rng = np.random.default_rng(random_state)

    if forest.bootstrap:
        n_samples_bootstrap = _get_n_samples_bootstrap(
            n_samples=X_train.shape[0], max_samples=forest.max_samples
        )
    else:
        n_samples_bootstrap = None

    # individual tree permutation of y labels
    if covariate_index is not None:
        indices = np.arange(X_train.shape[0], dtype=int)
        # perform permutation of covariates
        index_arr = rng.choice(indices, size=(X_train.shape[0], 1), replace=False, shuffle=True)
        perm_X_cov = X_train[index_arr, covariate_index]
        X_train[:, covariate_index] = perm_X_cov

    tree = _parallel_build_trees(
        tree,
        forest.bootstrap,
        X_train,
        y_train,
        sample_weight,
        idx,
        len(forest.estimators_),
        verbose=0,
        class_weight=class_weight,
        n_samples_bootstrap=n_samples_bootstrap,
        missing_values_in_feature_mask=missing_values_in_feature_mask,
        classes=classes,
    )

    if predict_posteriors:
        # XXX: currently assumes n_outputs_ == 1
        y_pred = tree.predict_proba(X[indices_test, :]).reshape(-1, tree.n_classes_)
    else:
        y_pred = tree.predict(X[indices_test, :]).reshape(-1, tree.n_outputs_)

    # Fill test set posteriors & set rest NaN
    posterior_arr[idx, indices_test, :] = y_pred  # posterior


class BaseForestHT(MetaEstimatorMixin):
    observe_samples_: ArrayLike
    observe_posteriors_: ArrayLike
    observe_stat_: float
    permute_samples_: ArrayLike
    permute_posteriors_: ArrayLike
    permute_stat_: float

    def __init__(
        self,
        estimator=None,
        random_state=None,
        verbose=0,
        test_size=0.2,
        permute_per_tree=True,
        sample_dataset_per_tree=True,
    ):
        self.estimator = estimator
        self.random_state = random_state
        self.verbose = verbose
        self.test_size = test_size
        self.permute_per_tree = permute_per_tree
        self.sample_dataset_per_tree = sample_dataset_per_tree

        self.n_samples_test_ = None
        self._n_samples_ = None
        self._metric = None
        self._covariate_index_cache_ = None
        self._type_of_target_ = None
        self.n_features_in_ = None
        self._is_fitted = False

    @property
    def n_estimators(self):
        return self.estimator_.n_estimators

    def reset(self):
        class_attributes = dir(type(self))
        instance_attributes = dir(self)

        for attr_name in instance_attributes:
            if attr_name.endswith("_") and attr_name not in class_attributes:
                delattr(self, attr_name)

        self.n_samples_test_ = None
        self._n_samples_ = None
        self._covariate_index_cache_ = None
        self._type_of_target_ = None
        self._metric = None
        self.n_features_in_ = None
        self._is_fitted = False

    def _get_estimators_indices(self):
        indices = np.arange(self._n_samples_, dtype=int)

        # Get drawn indices along both sample and feature axes
        if self.sample_dataset_per_tree:
            for tree in self.estimator_.estimators_:
                seed = tree.random_state

                # Operations accessing random_state must be performed identically
                # to those in `_parallel_build_trees()`
                indices_train, indices_test = train_test_split(
                    indices, test_size=self.test_size, shuffle=True, random_state=seed
                )

                yield indices_train, indices_test
        else:
            indices_train, indices_test = train_test_split(
                indices,
                test_size=self.test_size,
                shuffle=True,
                random_state=self.estimator_.random_state,
            )
            for tree in self.estimator_.estimators_:
                yield indices_train, indices_test

    @property
    def train_test_samples_(self):
        """
        The subset of drawn samples for each base estimator.

        Returns a dynamically generated list of indices identifying
        the samples used for fitting each member of the ensemble, i.e.,
        the in-bag samples.

        Note: the list is re-created at each call to the property in order
        to reduce the object memory footprint by not storing the sampling
        data. Thus fetching the property may be slower than expected.
        """
        if self._n_samples_ is None:
            raise RuntimeError("The estimator must be fitted before accessing this attribute.")

        return [
            (indices_train, indices_test)
            for indices_train, indices_test in self._get_estimators_indices()
        ]

    def _statistic(
        self,
        estimator: ForestClassifier,
        X: ArrayLike,
        y: ArrayLike,
        covariate_index: ArrayLike,
        metric: str,
        return_posteriors: bool,
        **metric_kwargs,
    ):
        raise NotImplementedError("Subclasses should implement this!")

    def _check_input(self, X: ArrayLike, y: ArrayLike, covariate_index: ArrayLike = None):
        X, y = check_X_y(X, y, ensure_2d=True, copy=True, multi_output=True)
        if y.ndim != 2:
            y = y.reshape(-1, 1)

        if covariate_index is not None:
            if not isinstance(covariate_index, (list, tuple, np.ndarray)):
                raise RuntimeError("covariate_index must be an iterable of integer indices")
            else:
                if not all(isinstance(idx, (np.integer, int)) for idx in covariate_index):
                    raise RuntimeError("Not all covariate_index are integer indices")

        if self.test_size * X.shape[0] < 5:
            raise RuntimeError(
                f"There are less than 5 testing samples used with "
                f"test_size={self.test_size} for X ({X.shape})."
            )

        if self._n_samples_ is not None and X.shape[0] != self._n_samples_:
            raise RuntimeError(
                f"X must have {self._n_samples_} samples, got {X.shape[0]}. "
                f"If running on a new dataset, call the 'reset' method."
            )
        if self.n_features_in_ is not None and X.shape[1] != self.n_features_in_:
            raise RuntimeError(
                f"X must have {self.n_features_in_} features, got {X.shape[1]}. "
                f"If running on a new dataset, call the 'reset' method."
            )
        if self._type_of_target_ is not None and type_of_target(y) != self._type_of_target_:
            raise RuntimeError(
                f"y must have type {self._type_of_target_}, got {type_of_target(y)}. "
                f"If running on a new dataset, call the 'reset' method."
            )

        return X, y, covariate_index

    def statistic(
        self,
        X: ArrayLike,
        y: ArrayLike,
        covariate_index: ArrayLike = None,
        metric="mi",
        return_posteriors: bool = False,
        check_input: bool = True,
        **metric_kwargs,
    ) -> Tuple[float, ArrayLike, ArrayLike]:
        """Compute the test statistic.

        Parameters
        ----------
        X : ArrayLike of shape (n_samples, n_features)
            The data matrix.
        y : ArrayLike of shape (n_samples, n_outputs)
            The target matrix.
        covariate_index : ArrayLike, optional of shape (n_covariates,)
            The index array of covariates to shuffle, by default None.
        metric : str, optional
            The metric to compute, by default "mse".
        return_posteriors : bool, optional
            Whether or not to return the posteriors, by default False.
        check_input : bool, optional
            Whether or not to check the input, by default True.
        **metric_kwargs : dict, optional
            Additional keyword arguments to pass to the metric function.

        Returns
        -------
        stat : float
            The test statistic.
        posterior_final : ArrayLike of shape (n_estimators, n_samples_final, n_outputs) or
            (n_estimators, n_samples_final), optional
            If ``return_posteriors`` is True, then the posterior probabilities of the
            samples used in the final test. ``n_samples_final`` is equal to ``n_samples``
            if all samples are encountered in the test set of at least one tree in the
            posterior computation.
        samples : ArrayLike of shape (n_samples_final,), optional
            The indices of the samples used in the final test. ``n_samples_final`` is
            equal to ``n_samples`` if all samples are encountered in the test set of at
            least one tree in the posterior computation.
        """
        if check_input:
            X, y, covariate_index = self._check_input(X, y, covariate_index)

        if self._n_samples_ is None:
            self._n_samples_, self.n_features_in_ = X.shape
        if self._type_of_target_ is None:
            self._type_of_target_ = type_of_target(y)

        # if self.sample_dataset_per_tree and not self.permute_per_tree:
        #     raise ValueError("sample_dataset_per_tree is only valid when permute_per_tree=True")

        if covariate_index is None:
            self.estimator_ = self._get_estimator()
            estimator = self.estimator_
        else:
            self.permuted_estimator_ = self._get_estimator()
            estimator = self.permuted_estimator_

        # Infer type of target y
        if not hasattr(self, "_type_of_target"):
            self._type_of_target_ = type_of_target(y)

        # XXX: this can be improved as an extra fit can be avoided, by just doing error-checking
        # and then setting the internal meta data structures
        # first run a dummy fit on the samples to initialize the
        # internal data structure of the forest
        if not _is_fitted(estimator) and is_classifier(estimator):
            _unique_y = []
            for axis in range(y.shape[1]):
                _unique_y.append(np.unique(y[:, axis]))
            unique_y = np.hstack(_unique_y)
            if unique_y.ndim > 1 and unique_y.shape[1] == 1:
                unique_y = unique_y.ravel()
            X_dummy = np.zeros((unique_y.shape[0], X.shape[1]))
            estimator.fit(X_dummy, unique_y)
        elif not _is_fitted(estimator):
            if y.ndim > 1 and y.shape[1] == 1:
                estimator.fit(X[:2], y[:2].ravel())
            else:
                estimator.fit(X[:2], y[:2])

        # sampling a separate train/test per tree
        if self.sample_dataset_per_tree:
            self.n_samples_test_ = self._n_samples_
        else:
            # here we fix a training/testing dataset
            test_size_ = int(self.test_size * self._n_samples_)

            # Fit each tree and compute posteriors with train test splits
            self.n_samples_test_ = test_size_

        if self._metric is not None and self._metric != metric:
            raise RuntimeError(
                f"Metric must be {self._metric}, got {metric}. "
                f"If running on a new dataset, call the 'reset' method."
            )
        self._metric = metric

        if not is_classifier(self.estimator_) and metric not in REGRESSOR_METRICS:
            raise RuntimeError(
                f'Metric must be either "mse" or "mae" if using Regression, got {metric}'
            )

        if estimator.n_outputs_ > 1 and metric == "auc":
            raise ValueError("AUC metric is not supported for multi-output")

        return self._statistic(
            estimator,
            X,
            y,
            covariate_index=covariate_index,
            metric=metric,
            return_posteriors=return_posteriors,
            **metric_kwargs,
        )

    def test(
        self,
        X,
        y,
        covariate_index: ArrayLike = None,
        metric: str = "mi",
        n_repeats: int = 1000,
        return_posteriors: bool = True,
        **metric_kwargs,
    ):
        """Perform hypothesis test using Coleman method.

        X is split into a training/testing split. Optionally, the covariate index
        columns are shuffled.

        On the training dataset, two honest forests are trained and then the posterior
        is estimated on the testing dataset. One honest forest is trained on the
        permuted dataset and the other is trained on the original dataset.

        Finally, resample the posteriors of the two forests to compute the null
        distribution of the statistics.

        Parameters
        ----------
        X : ArrayLike of shape (n_samples, n_features)
            The data matrix.
        y : ArrayLike of shape (n_samples, n_outputs)
            The target matrix.
        covariate_index : ArrayLike, optional of shape (n_covariates,)
            The index array of covariates to shuffle, by default None.
        metric : str, optional
            The metric to compute, by default "mse".
        n_repeats : int, optional
            Number of times to sample the null distribution, by default 1000.
        return_posteriors : bool, optional
            Whether or not to return the posteriors, by default True.
        **metric_kwargs : dict, optional
            Additional keyword arguments to pass to the metric function.

        Returns
        -------
        stat : float
            The test statistic.
        pval : float
            The p-value of the test statistic.
        """
        X, y, covariate_index = self._check_input(X, y, covariate_index)

        if not self._is_fitted:
            # first compute the test statistic on the un-permuted data
            observe_stat, observe_posteriors, observe_samples = self.statistic(
                X,
                y,
                covariate_index=None,
                metric=metric,
                return_posteriors=return_posteriors,
                check_input=False,
                **metric_kwargs,
            )
        else:
            observe_samples = self.observe_samples_
            observe_posteriors = self.observe_posteriors_
            observe_stat = self.observe_stat_

        # next permute the data
        permute_stat, permute_posteriors, permute_samples = self.statistic(
            X,
            y,
            covariate_index=covariate_index,
            metric=metric,
            return_posteriors=return_posteriors,
            check_input=False,
            **metric_kwargs,
        )
        self.permute_stat_ = permute_stat

        # Note: at this point, both `estimator` and `permuted_estimator_` should
        # have been fitted already, so we can now compute on the null by resampling
        # the posteriors and computing the test statistic on the resampled posteriors
        if self.sample_dataset_per_tree:
            metric_star, metric_star_pi = _compute_null_distribution_coleman(
                y_test=y,
                y_pred_proba_normal=observe_posteriors,
                y_pred_proba_perm=permute_posteriors,
                metric=metric,
                n_repeats=n_repeats,
                seed=self.random_state,
            )
        else:
            # If not sampling a new dataset per tree, then we may either be
            # permuting the covariate index per tree or per forest. If not permuting
            # there is only one train and test split, so we can just use that
            _, indices_test = self.train_test_samples_[0]
            y_test = y[indices_test, :]
            y_pred_proba_normal = observe_posteriors[:, indices_test, :]
            y_pred_proba_perm = permute_posteriors[:, indices_test, :]

            metric_star, metric_star_pi = _compute_null_distribution_coleman(
                y_test=y_test,
                y_pred_proba_normal=y_pred_proba_normal,
                y_pred_proba_perm=y_pred_proba_perm,
                metric=metric,
                n_repeats=n_repeats,
                seed=self.random_state,
            )
        # metric^\pi - metric = observed test statistic, which under the
        # null is normally distributed around 0
        observe_stat = permute_stat - observe_stat

        # metric^\pi_j - metric_j, which is centered at 0
        null_dist = metric_star_pi - metric_star

        # compute pvalue
        if metric in POSITIVE_METRICS:
            pvalue = (1 + (null_dist <= observe_stat).sum()) / (1 + n_repeats)
        else:
            pvalue = (1 + (null_dist >= observe_stat).sum()) / (1 + n_repeats)

        if return_posteriors:
            self.observe_posteriors_ = observe_posteriors
            self.permute_posteriors_ = permute_posteriors
            self.observe_samples_ = observe_samples
            self.permute_samples_ = permute_samples

        self.null_dist_ = null_dist
        return observe_stat, pvalue


class FeatureImportanceForestRegressor(BaseForestHT):
    """Forest hypothesis testing with continuous `y` variable.

    Implements the algorithm described in :footcite:`coleman2022scalable`.

    The dataset is split into a training and testing dataset initially. Then there
    are two forests that are trained: one on the original dataset, and one on the
    permuted dataset. The dataset is either permuted once, or independently for
    each tree in the permuted forest. The original test statistic is computed by
    comparing the metric on both forests ``(metric_forest - metric_perm_forest)``.

    Then the output predictions are randomly sampled to recompute the test statistic
    ``n_repeats`` times. The p-value is computed as the proportion of times the
    null test statistic is greater than the original test statistic.

    Parameters
    ----------
    estimator : object, default=None
        Type of forest estimator to use. By default `None`, which defaults to
        :class:`sklearn.ensemble.RandomForestRegressor`.

    random_state : int, RandomState instance or None, default=None
        Controls both the randomness of the bootstrapping of the samples used
        when building trees (if ``bootstrap=True``) and the sampling of the
        features to consider when looking for the best split at each node
        (if ``max_features < n_features``).
        See :term:`Glossary <random_state>` for details.

    verbose : int, default=0
        Controls the verbosity when fitting and predicting.

    test_size : float, default=0.2
        Proportion of samples per tree to use for the test set.

    permute_per_tree : bool, default=True
        Whether to permute the covariate index per tree or per forest.

    sample_dataset_per_tree : bool, default=False
        Whether to sample the dataset per tree or per forest.

    Attributes
    ----------
    estimator_ : BaseForest
        The estimator used to compute the test statistic.

    n_samples_test_ : int
        The number of samples used in the final test set.

    indices_train_ : ArrayLike of shape (n_samples_train,)
        The indices of the samples used in the training set.

    indices_test_ : ArrayLike of shape (n_samples_test,)
        The indices of the samples used in the testing set.

    samples_ : ArrayLike of shape (n_samples_final,)
        The indices of the samples used in the final test set that would slice
        the original ``(X, y)`` input.

    y_true_final_ : ArrayLike of shape (n_samples_final,)
        The true labels of the samples used in the final test.

    observe_posteriors_ : ArrayLike of shape (n_estimators, n_samples, n_outputs) or
        (n_estimators, n_samples, n_classes)
        The predicted posterior probabilities of the samples used in the final test.
        For samples that are NaNs for all estimators, means the sample was not used
        in the test set at all across all trees.

    null_dist_ : ArrayLike of shape (n_repeats,)
        The null distribution of the test statistic.

    Notes
    -----
    This class trains two forests: one on the original dataset, and one on the
    permuted dataset. The forest from the original dataset is cached and re-used to
    compute the test-statistic each time the :meth:`test` method is called. However,
    the forest from the permuted dataset is re-trained each time the :meth:`test` is called
    if the ``covariate_index`` differs from the previous run.

    To fully start from a new dataset, call the ``reset`` method, which will then
    re-train both forests upon calling the :meth:`test` and :meth:`statistic` methods.

    References
    ----------
    .. footbibliography::
    """

    def __init__(
        self,
        estimator=None,
        random_state=None,
        verbose=0,
        test_size=0.2,
        permute_per_tree=True,
        sample_dataset_per_tree=True,
    ):
        super().__init__(
            estimator=estimator,
            random_state=random_state,
            verbose=verbose,
            test_size=test_size,
            permute_per_tree=permute_per_tree,
            sample_dataset_per_tree=sample_dataset_per_tree,
        )

    def _get_estimator(self):
        if self.estimator is None:
            estimator_ = RandomForestRegressor()
        elif not isinstance(self.estimator, (ForestRegressor, sklearnForestRegressor)):
            raise RuntimeError(f"Estimator must be a ForestRegressor, got {type(self.estimator)}")
        else:
            estimator_ = self.estimator
        return clone(estimator_)

    def statistic(
        self,
        X: ArrayLike,
        y: ArrayLike,
        covariate_index: ArrayLike = None,
        metric="mse",
        return_posteriors: bool = False,
        check_input: bool = True,
        **metric_kwargs,
    ):
        """Compute the test statistic.

        Parameters
        ----------
        X : ArrayLike of shape (n_samples, n_features)
            The data matrix.
        y : ArrayLike of shape (n_samples, n_outputs)
            The target matrix.
        covariate_index : ArrayLike, optional of shape (n_covariates,)
            The index array of covariates to shuffle, by default None.
        metric : str, optional
            The metric to compute, by default "mse".
        return_posteriors : bool, optional
            Whether or not to return the posteriors, by default False.
        check_input : bool, optional
            Whether or not to check the input, by default True.
        **metric_kwargs : dict, optional
            Additional keyword arguments to pass to the metric function.

        Returns
        -------
        stat : float
            The test statistic.
        posterior_final : ArrayLike of shape (n_estimators, n_samples_final, n_outputs) or
            (n_estimators, n_samples_final), optional
            If ``return_posteriors`` is True, then the posterior probabilities of the
            samples used in the final test. ``n_samples_final`` is equal to ``n_samples``
            if all samples are encountered in the test set of at least one tree in the
            posterior computation.
        samples : ArrayLike of shape (n_samples_final,), optional
            The indices of the samples used in the final test. ``n_samples_final`` is
            equal to ``n_samples`` if all samples are encountered in the test set of at
            least one tree in the posterior computation.
        """
        return super().statistic(
            X, y, covariate_index, metric, return_posteriors, check_input, **metric_kwargs
        )

    def _statistic(
        self,
        estimator: ForestClassifier,
        X: ArrayLike,
        y: ArrayLike,
        covariate_index: ArrayLike,
        metric: str,
        return_posteriors: bool,
        **metric_kwargs,
    ):
        """Helper function to compute the test statistic."""
        metric_func: Callable[[ArrayLike, ArrayLike], float] = METRIC_FUNCTIONS[metric]
        rng = np.random.default_rng(self.random_state)

        posterior_arr = np.full((self.n_estimators, self._n_samples_, estimator.n_outputs_), np.nan)

        # both sampling dataset per tree or permuting per tree requires us to bypass the
        # sklearn API to fit each tree individually
        if self.sample_dataset_per_tree or self.permute_per_tree:
            Parallel(n_jobs=estimator.n_jobs, verbose=self.verbose, prefer="threads")(
                delayed(_parallel_build_trees_and_compute_posteriors)(
                    estimator,
                    idx,
                    indices_train,
                    indices_test,
                    X,
                    y,
                    covariate_index,
                    posterior_arr,
                    False,
                    self.permute_per_tree,
                )
                for idx, (indices_train, indices_test) in enumerate(self._get_estimators_indices())
            )
        else:
            # fitting a forest will only get one unique train/test split
            indices_train, indices_test = self.train_test_samples_[0]

            X_train, X_test = X[indices_train, :], X[indices_test, :]
            y_train, y_test = y[indices_train, :], y[indices_test, :]

            if covariate_index is not None:
                # perform permutation of covariates
                n_samples_train = X_train.shape[0]
                index_arr = rng.choice(
                    np.arange(n_samples_train, dtype=int),
                    size=(n_samples_train, 1),
                    replace=False,
                    shuffle=True,
                )
                X_train[:, covariate_index] = X_train[index_arr, covariate_index]

            if self._type_of_target_ == "binary":
                y_train = y_train.ravel()
            estimator.fit(X_train, y_train)

            # construct posterior array for all trees (n_trees, n_samples_test, n_outputs)
            for itree, tree in enumerate(estimator.estimators_):
                posterior_arr[itree, indices_test, ...] = tree.predict(X_test).reshape(
                    -1, tree.n_outputs_
                )

            # set variables to compute metric
            samples = indices_test
            y_true_final = y_test

        # determine if there are any nans in the final posterior array, when
        # averaged over the trees
        samples = _non_nan_samples(posterior_arr)

        # Ignore all NaN values (samples not tested)
        y_true_final = y[(samples), :]

        # Average all posteriors (n_samples_test, n_outputs) to compute the statistic
        posterior_forest = np.nanmean(posterior_arr[:, (samples), :], axis=0)
        stat = metric_func(y_true_final, posterior_forest, **metric_kwargs)
        if covariate_index is None:
            # Ignore all NaN values (samples not tested) -> (n_samples_final, n_outputs)
            # arrays of y and predicted posterior
            self.observe_samples_ = samples
            self.y_true_final_ = y_true_final
            self.observe_posteriors_ = posterior_arr
            self.observe_stat_ = stat
            self._is_fitted = True

        if return_posteriors:
            return stat, posterior_arr, samples

        return stat


class FeatureImportanceForestClassifier(BaseForestHT):
    """Forest hypothesis testing with categorical `y` variable.

    Implements the algorithm described in :footcite:`coleman2022scalable`.

    The dataset is split into a training and testing dataset initially. Then there
    are two forests that are trained: one on the original dataset, and one on the
    permuted dataset. The dataset is either permuted once, or independently for
    each tree in the permuted forest. The original test statistic is computed by
    comparing the metric on both forests ``(metric_forest - metric_perm_forest)``.

    Then the output predictions are randomly sampled to recompute the test statistic
    ``n_repeats`` times. The p-value is computed as the proportion of times the
    null test statistic is greater than the original test statistic.

    Parameters
    ----------
    estimator : object, default=None
        Type of forest estimator to use. By default `None`, which defaults to
        :class:`sklearn.ensemble.RandomForestRegressor`.

    random_state : int, RandomState instance or None, default=None
        Controls both the randomness of the bootstrapping of the samples used
        when building trees (if ``bootstrap=True``) and the sampling of the
        features to consider when looking for the best split at each node
        (if ``max_features < n_features``).
        See :term:`Glossary <random_state>` for details.

    verbose : int, default=0
        Controls the verbosity when fitting and predicting.

    test_size : float, default=0.2
        Proportion of samples per tree to use for the test set.

    permute_per_tree : bool, default=True
        Whether to permute the covariate index per tree or per forest.

    sample_dataset_per_tree : bool, default=False
        Whether to sample the dataset per tree or per forest.

    Attributes
    ----------
    estimator_ : BaseForest
        The estimator used to compute the test statistic.

    n_samples_test_ : int
        The number of samples used in the final test set.

    indices_train_ : ArrayLike of shape (n_samples_train,)
        The indices of the samples used in the training set.

    indices_test_ : ArrayLike of shape (n_samples_test,)
        The indices of the samples used in the testing set.

    samples_ : ArrayLike of shape (n_samples_final,)
        The indices of the samples used in the final test set that would slice
        the original ``(X, y)`` input along the rows.

    y_true_final_ : ArrayLike of shape (n_samples_final,)
        The true labels of the samples used in the final test.

    observe_posteriors_ : ArrayLike of shape (n_estimators, n_samples_final, n_outputs) or
        (n_estimators, n_samples_final, n_classes)
        The predicted posterior probabilities of the samples used in the final test.

    null_dist_ : ArrayLike of shape (n_repeats,)
        The null distribution of the test statistic.

    Notes
    -----
    This class trains two forests: one on the original dataset, and one on the
    permuted dataset. The forest from the original dataset is cached and re-used to
    compute the test-statistic each time the :meth:`test` method is called. However,
    the forest from the permuted dataset is re-trained each time the :meth:`test` is called
    if the ``covariate_index`` differs from the previous run.

    To fully start from a new dataset, call the ``reset`` method, which will then
    re-train both forests upon calling the :meth:`test` and :meth:`statistic` methods.

    References
    ----------
    .. footbibliography::
    """

    def __init__(
        self,
        estimator=None,
        random_state=None,
        verbose=0,
        test_size=0.2,
        permute_per_tree=True,
        sample_dataset_per_tree=True,
    ):
        super().__init__(
            estimator=estimator,
            random_state=random_state,
            verbose=verbose,
            test_size=test_size,
            permute_per_tree=permute_per_tree,
            sample_dataset_per_tree=sample_dataset_per_tree,
        )

    def _get_estimator(self):
        if self.estimator is None:
            estimator_ = RandomForestClassifier()
        elif not isinstance(self.estimator, (ForestClassifier, sklearnForestClassifier)):
            raise RuntimeError(f"Estimator must be a ForestClassifier, got {type(self.estimator)}")
        else:
            # self.estimator is an instance of a ForestEstimator
            estimator_ = self.estimator
        return clone(estimator_)

    def _statistic(
        self,
        estimator: ForestClassifier,
        X: ArrayLike,
        y: ArrayLike,
        covariate_index: ArrayLike,
        metric: str,
        return_posteriors: bool,
        **metric_kwargs,
    ):
        """Helper function to compute the test statistic."""
        metric_func: Callable[[ArrayLike, ArrayLike], float] = METRIC_FUNCTIONS[metric]
        rng = np.random.default_rng(estimator.random_state)

        if metric in POSTERIOR_FUNCTIONS:
            predict_posteriors = True
        else:
            predict_posteriors = False

        if predict_posteriors:
            # now initialize posterior array as (n_trees, n_samples_test, n_classes)
            # XXX: currently assumes n_outputs_ == 1
            posterior_arr = np.full(
                (self.n_estimators, self._n_samples_, estimator.n_classes_), np.nan
            )
        else:
            # now initialize posterior array as (n_trees, n_samples_test, n_outputs)
            posterior_arr = np.full(
                (self.n_estimators, self._n_samples_, estimator.n_outputs_), np.nan
            )

        # both sampling dataset per tree or permuting per tree requires us to bypass the
        # sklearn API to fit each tree individually
        if self.sample_dataset_per_tree or self.permute_per_tree:
            Parallel(n_jobs=estimator.n_jobs, verbose=self.verbose, prefer="threads")(
                delayed(_parallel_build_trees_and_compute_posteriors)(
                    estimator,
                    idx,
                    indices_train,
                    indices_test,
                    X,
                    y,
                    covariate_index,
                    posterior_arr,
                    predict_posteriors,
                    self.permute_per_tree,
                )
                for idx, (indices_train, indices_test) in enumerate(self._get_estimators_indices())
            )
        else:
            # fitting a forest will only get one unique train/test split
            indices_train, indices_test = self.train_test_samples_[0]

            X_train, X_test = X[indices_train, :], X[indices_test, :]
            y_train, y_test = y[indices_train, :], y[indices_test, :]

            if covariate_index is not None:
                # perform permutation of covariates
                n_samples_train = X_train.shape[0]
                index_arr = rng.choice(
                    np.arange(n_samples_train, dtype=int),
                    size=(n_samples_train, 1),
                    replace=False,
                    shuffle=True,
                )
                X_train[:, covariate_index] = X_train[index_arr, covariate_index]

            if self._type_of_target_ == "binary":
                y_train = y_train.ravel()
            estimator.fit(X_train, y_train)

            # construct posterior array for all trees (n_trees, n_samples_test, n_outputs)
            for itree, tree in enumerate(estimator.estimators_):
                if predict_posteriors:
                    # XXX: currently assumes n_outputs_ == 1
                    posterior_arr[itree, indices_test, ...] = tree.predict_proba(X_test).reshape(
                        -1, tree.n_classes_
                    )
                else:
                    posterior_arr[itree, indices_test, ...] = tree.predict(X_test).reshape(
                        -1, tree.n_outputs_
                    )

            # set variables to compute metric
            samples = indices_test
            y_true_final = y_test
        if metric == "auc":
            # at this point, posterior_final is the predicted posterior for only the positive class
            # as more than one output is not supported.
            if self._type_of_target_ == "binary":
                posterior_arr = posterior_arr[..., (1,)]
            else:
                raise RuntimeError(
                    f"AUC metric is not supported for {self._type_of_target_} targets."
                )

        # determine if there are any nans in the final posterior array, when
        # averaged over the trees
        samples = _non_nan_samples(posterior_arr)

        # Ignore all NaN values (samples not tested)
        y_true_final = y[(samples), :]

        # Average all posteriors (n_samples_test, n_outputs) to compute the statistic
        posterior_forest = np.nanmean(posterior_arr[:, (samples), :], axis=0)
        stat = metric_func(y_true_final, posterior_forest, **metric_kwargs)

        if covariate_index is None:
            # Ignore all NaN values (samples not tested) -> (n_samples_final, n_outputs)
            # arrays of y and predicted posterior
            self.observe_samples_ = samples
            self.y_true_final_ = y_true_final
            self.observe_posteriors_ = posterior_arr
            self.observe_stat_ = stat
            self._is_fitted = True

        if return_posteriors:
            return stat, posterior_arr, samples

        return stat