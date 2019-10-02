# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

"""Double ML.

"Double Machine Learning" is an algorithm that applies arbitrary machine learning methods
to fit the treatment and response, then uses a linear model to predict the response residuals
from the treatment residuals.

"""

import numpy as np
import copy
from warnings import warn
from .utilities import (shape, reshape, ndim, hstack, cross_product, transpose,
                        broadcast_unit_treatments, reshape_treatmentwise_effects,
                        StatsModelsLinearRegression, LassoCVWrapper)
from sklearn.model_selection import KFold, StratifiedKFold, check_cv
from sklearn.linear_model import LinearRegression, LassoCV
from sklearn.preprocessing import (PolynomialFeatures, LabelEncoder, OneHotEncoder,
                                   FunctionTransformer)
from sklearn.base import clone, TransformerMixin
from sklearn.pipeline import Pipeline
from sklearn.utils import check_random_state
from .cate_estimator import (BaseCateEstimator, LinearCateEstimator,
                             TreatmentExpansionMixin, StatsModelsCateEstimatorMixin)
from .inference import StatsModelsInference


class _RLearner(TreatmentExpansionMixin, LinearCateEstimator):
    """
    Base class for orthogonal learners.

    Parameters
    ----------
    model_y: estimator
        The estimator for fitting the response to the features and controls. Must implement
        `fit` and `predict` methods.  Unlike sklearn estimators both methods must
        take an extra second argument (the controls).

    model_t: estimator
        The estimator for fitting the treatment to the features and controls. Must implement
        `fit` and `predict` methods.  Unlike sklearn estimators both methods must
        take an extra second argument (the controls).

    model_final: estimator for fitting the response residuals to the features and treatment residuals
        Must implement `fit` and `predict` methods. Unlike sklearn estimators the fit methods must
        take an extra second argument (the treatment residuals).  Predict, on the other hand,
        should just take the features and return the constant marginal effect.

    discrete_treatment: bool
        Whether the treatment values should be treated as categorical, rather than continuous, quantities

    n_splits: int, cross-validation generator or an iterable, optional
        Determines the cross-validation splitting strategy.
        Possible inputs for cv are:

        - None, to use the default 3-fold cross-validation,
        - integer, to specify the number of folds.
        - :term:`CV splitter`
        - An iterable yielding (train, test) splits as arrays of indices.

        For integer/None inputs, if the treatment is discrete
        :class:`~sklearn.model_selection.StratifiedKFold` is used, else,
        :class:`~sklearn.model_selection.KFold` is used
        (with a random shuffle in either case).

    random_state: int, :class:`~numpy.random.mtrand.RandomState` instance or None
        If int, random_state is the seed used by the random number generator;
        If :class:`~numpy.random.mtrand.RandomState` instance, random_state is the random number generator;
        If None, the random number generator is the :class:`~numpy.random.mtrand.RandomState` instance used
        by `np.random`.
    """

    def __init__(self, model_y, model_t, model_final,
                 discrete_treatment, n_splits, random_state):
        self._model_y = clone(model_y, safe=False)
        self._model_t = clone(model_t, safe=False)
        self._models_y = []
        self._models_t = []
        self._model_final = clone(model_final, safe=False)
        self._n_splits = n_splits
        self._discrete_treatment = discrete_treatment
        self._random_state = check_random_state(random_state)
        if discrete_treatment:
            self._label_encoder = LabelEncoder()
            self._one_hot_encoder = OneHotEncoder(categories='auto', sparse=False)
        super().__init__()

    @staticmethod
    def _check_X_W(X, W, Y):
        if X is None:
            X = np.ones((shape(Y)[0], 1))
        if W is None:
            W = np.empty((shape(Y)[0], 0))
        return X, W

    @BaseCateEstimator._wrap_fit
    def fit(self, Y, T, X=None, W=None, sample_weight=None, sample_var=None, inference=None):
        """
        Estimate the counterfactual model from data, i.e. estimates functions τ(·,·,·), ∂τ(·,·).

        Parameters
        ----------
        Y: (n × d_y) matrix or vector of length n
            Outcomes for each sample
        T: (n × dₜ) matrix or vector of length n
            Treatments for each sample
        X: optional (n × dₓ) matrix
            Features for each sample
        W: optional (n × d_w) matrix
            Controls for each sample
        sample_weight: optional (n,) vector
            Weights for each row
        inference: string, `Inference` instance, or None
            Method for performing inference.  This estimator supports 'bootstrap'
            (or an instance of `BootstrapInference`).

        Returns
        -------
        self
        """
        X, W = self._check_X_W(X, W, Y)
        assert shape(Y)[0] == shape(T)[0] == shape(X)[0] == shape(W)[0]

        Y_res, T_res = self.fit_nuisances(Y, T, X, W, sample_weight=sample_weight)

        self.fit_final(X, Y_res, T_res, sample_weight=sample_weight, sample_var=sample_var)

    def fit_nuisances(self, Y, T, X, W, sample_weight=None):
        # use a binary array to get stratified split in case of discrete treatment
        splitter = check_cv(self._n_splits, [0], classifier=self._discrete_treatment)
        # if check_cv produced a new KFold or StratifiedKFold object, we need to set shuffle and random_state
        if splitter != self._n_splits and isinstance(splitter, (KFold, StratifiedKFold)):
            splitter.shuffle = True
            splitter.random_state = self._random_state

        folds = splitter.split(X, T)

        if self._discrete_treatment:
            T = self._label_encoder.fit_transform(T)
            T_out = self._one_hot_encoder.fit_transform(reshape(T, (-1, 1)))
            T_out = T_out[:, 1:]  # drop first column since all columns sum to one
            self._d_t = shape(T_out)[1:]
            self.transformer = FunctionTransformer(
                func=(lambda T:
                      self._one_hot_encoder.transform(
                          reshape(self._label_encoder.transform(T), (-1, 1)))[:, 1:]),
                validate=False)
        else:
            T_out = T

        Y_res = np.zeros(shape(Y))
        T_res = np.zeros(shape(T_out))
        for idx, (train_idxs, test_idxs) in enumerate(folds):
            self._models_y.append(clone(self._model_y, safe=False))
            self._models_t.append(clone(self._model_t, safe=False))
            Y_train, Y_test = Y[train_idxs], Y[test_idxs]
            T_train, T_test = T[train_idxs], T_out[test_idxs]
            X_train, X_test = X[train_idxs], X[test_idxs]
            W_train, W_test = W[train_idxs], W[test_idxs]
            # TODO: If T is a vector rather than a 2-D array, then the model's fit must accept a vector...
            #       Do we want to reshape to an nx1, or just trust the user's choice of input?
            #       (Likewise for Y below)
            if sample_weight is not None:
                self._models_t[idx].fit(X_train, W_train, T_train, sample_weight=sample_weight[train_idxs])
            else:
                self._models_t[idx].fit(X_train, W_train, T_train)
            if self._discrete_treatment:
                T_pred = self._models_t[idx].predict(X_test, W_test)[:, 1:]
            else:
                T_pred = self._models_t[idx].predict(X_test, W_test)
            if shape(T_pred) != shape(T_test):
                T_pred = reshape(T_pred, shape(T_test))
            T_res[test_idxs] = T_test - T_pred
            if sample_weight is not None:
                self._models_y[idx].fit(X_train, W_train, Y_train, sample_weight=sample_weight[train_idxs])
            else:
                self._models_y[idx].fit(X_train, W_train, Y_train)
            Y_pred = self._models_y[idx].predict(X_test, W_test)
            if shape(Y_pred) != shape(Y_test):
                Y_pred = reshape(Y_pred, shape(Y_test))
            Y_res[test_idxs] = Y_test - Y_pred
        return Y_res, T_res

    def fit_final(self, X, Y_res, T_res, sample_weight=None, sample_var=None):
        if sample_weight is not None:
            if sample_var is None:
                self._model_final.fit(X, T_res, Y_res, sample_weight=sample_weight)
            else:
                self._model_final.fit(X, T_res, Y_res, sample_weight=sample_weight, sample_var=sample_var)
        else:
            self._model_final.fit(X, T_res, Y_res)

    def const_marginal_effect(self, X=None):
        """
        Calculate the constant marginal CATE θ(·).

        The marginal effect is conditional on a vector of
        features on a set of m test samples {Xᵢ}.

        Parameters
        ----------
        X: optional (m × dₓ) matrix
            Features for each sample.
            If X is None, it will be treated as a column of ones with a single row

        Returns
        -------
        theta: (m × d_y × dₜ) matrix
            Constant marginal CATE of each treatment on each outcome for each sample.
            Note that when Y or T is a vector rather than a 2-dimensional array,
            the corresponding singleton dimensions in the output will be collapsed
            (e.g. if both are vectors, then the output of this method will also be a vector)
        """
        if X is None:
            X = np.ones((1, 1))
        return self._model_final.predict(X)

    def const_marginal_effect_interval(self, X=None, *, alpha=0.1):
        if X is None:
            X = np.ones((1, 1))
        return super().const_marginal_effect_interval(X, alpha=alpha)

    def score(self, Y, T, X=None, W=None):
        X, W = self._check_X_W(X, W, Y)
        (T,) = self._expand_treatments(X, T)
        if T.ndim == 1:
            T = reshape(T, (-1, 1))
        if Y.ndim == 1:
            Y = reshape(Y, (-1, 1))
        Y_test_pred = np.zeros(shape(Y) + (self._n_splits,))
        T_test_pred = np.zeros(shape(T) + (self._n_splits,))
        for ind in range(self._n_splits):
            if self._discrete_treatment:
                T_test_pred[:, :, ind] = reshape(self._models_t[ind].predict(X, W)[:, 1:], shape(T))
            else:
                T_test_pred[:, :, ind] = reshape(self._models_t[ind].predict(X, W), shape(T))
            Y_test_pred[:, :, ind] = reshape(self._models_y[ind].predict(X, W), shape(Y))
        Y_test_pred = Y_test_pred.mean(axis=2)
        T_test_pred = T_test_pred.mean(axis=2)
        Y_test_res = Y - Y_test_pred
        T_test_res = T - T_test_pred
        effects = reshape(self._model_final.predict(X), (-1, shape(Y)[1], shape(T)[1]))
        Y_test_res_pred = reshape(np.einsum('ijk,ik->ij', effects, T_test_res), shape(Y))
        mse = ((Y_test_res - Y_test_res_pred)**2).mean()
        return mse


class DMLCateEstimator(_RLearner):
    """
    The base class for parametric Double ML estimators.

    Parameters
    ----------
    model_y: estimator
        The estimator for fitting the response to the features. Must implement
        `fit` and `predict` methods.  Must be a linear model for correctness when linear_first_stages is ``True``.

    model_t: estimator
        The estimator for fitting the treatment to the features. Must implement
        `fit` and `predict` methods.  Must be a linear model for correctness when linear_first_stages is ``True``.

    model_final: estimator
        The estimator for fitting the response residuals to the treatment residuals. Must implement
        `fit` and `predict` methods, and must be a linear model for correctness.

    featurizer: transformer
        The transformer used to featurize the raw features when fitting the final model.  Must implement
        a `fit_transform` method.

    linear_first_stages: bool
        Whether the first stage models are linear (in which case we will expand the features passed to
        `model_y` accordingly)

    discrete_treatment: bool, optional (default is ``False``)
        Whether the treatment values should be treated as categorical, rather than continuous, quantities

    n_splits: int, cross-validation generator or an iterable, optional
        Determines the cross-validation splitting strategy.
        Possible inputs for cv are:

        - None, to use the default 3-fold cross-validation,
        - integer, to specify the number of folds.
        - :term:`CV splitter`
        - An iterable yielding (train, test) splits as arrays of indices.

        For integer/None inputs, if the treatment is discrete
        :class:`~sklearn.model_selection.StratifiedKFold` is used, else,
        :class:`~sklearn.model_selection.KFold` is used
        (with a random shuffle in either case).

    random_state: int, :class:`~numpy.random.mtrand.RandomState` instance or None, optional (default=None)
        If int, random_state is the seed used by the random number generator;
        If :class:`~numpy.random.mtrand.RandomState` instance, random_state is the random number generator;
        If None, the random number generator is the :class:`~numpy.random.mtrand.RandomState` instance used
        by `np.random`.
    """

    def __init__(self,
                 model_y, model_t, model_final,
                 featurizer,
                 linear_first_stages=False,
                 discrete_treatment=False,
                 n_splits=2,
                 random_state=None):

        # TODO: consider whether we need more care around stateful featurizers,
        #       since we clone it and fit separate copies

        class FirstStageWrapper:
            def __init__(self, model, is_Y):
                self._model = clone(model, safe=False)
                self._featurizer = clone(featurizer, safe=False)
                self._is_Y = is_Y

            def _combine(self, X, W, fitting=True):
                if self._is_Y and linear_first_stages:
                    F = self._featurizer.fit_transform(X) if fitting else self._featurizer.transform(X)
                    XW = hstack([X, W])
                    return cross_product(XW, hstack([np.ones((shape(XW)[0], 1)), F, W]))
                else:
                    return hstack([X, W])

            def fit(self, X, W, Target, sample_weight=None):
                if sample_weight is not None:
                    self._model.fit(self._combine(X, W), Target, sample_weight=sample_weight)
                else:
                    self._model.fit(self._combine(X, W), Target)

            def predict(self, X, W):
                if (not self._is_Y) and discrete_treatment:
                    return self._model.predict_proba(self._combine(X, W, fitting=False))
                else:
                    return self._model.predict(self._combine(X, W, fitting=False))

        class FinalWrapper:
            def __init__(self):
                self._model = clone(model_final, safe=False)
                self._featurizer = clone(featurizer, safe=False)

            def fit(self, X, T_res, Y_res, sample_weight=None, sample_var=None):
                # Track training dimensions to see if Y or T is a vector instead of a 2-dimensional array
                self._d_t = shape(T_res)[1:]
                self._d_y = shape(Y_res)[1:]
                fts = self._combine(X, T_res)
                if sample_weight is not None:
                    if sample_var is not None:
                        self._model.fit(fts,
                                        Y_res, sample_weight=sample_weight, sample_var=sample_var)
                    else:
                        self._model.fit(fts,
                                        Y_res, sample_weight=sample_weight)
                else:
                    self._model.fit(fts, Y_res)

                self._intercept = None
                intercept = self._model.predict(np.zeros_like(fts[0:1]))
                if (np.count_nonzero(intercept) > 0):
                    warn("The final model has a nonzero intercept for at least one outcome; "
                         "it will be subtracted, but consider fitting a model without an intercept if possible.",
                         UserWarning)
                    self._intercept = intercept

            def _combine(self, X, T, fitting=True):
                F = self._featurizer.fit_transform(X) if fitting else self._featurizer.transform(X)
                return cross_product(F, T)

            def predict(self, X):
                X, T = broadcast_unit_treatments(X, self._d_t[0] if self._d_t else 1)
                prediction = self._model.predict(self._combine(X, T, fitting=False))
                return reshape_treatmentwise_effects(prediction - self._intercept if self._intercept else prediction,
                                                     self._d_t, self._d_y)

            @property
            def coef_(self):
                # TODO: handle case where final model doesn't directly expose coef_?
                return reshape(self._model.coef_, self._d_y + self._d_t + (-1,))

        super().__init__(model_y=FirstStageWrapper(model_y, is_Y=True),
                         model_t=FirstStageWrapper(model_t, is_Y=False),
                         model_final=FinalWrapper(),
                         discrete_treatment=discrete_treatment,
                         n_splits=n_splits,
                         random_state=random_state)

    @property
    def featurizer(self):
        return self._model_final._featurizer


class LinearDMLCateEstimator(StatsModelsCateEstimatorMixin, DMLCateEstimator):
    """
    The Double ML Estimator with a low-dimensional linear final stage implemented as a statsmodel regression.

    Note that only a single outcome is supported (because of the dependency on statsmodel's weighted least squares)

    Parameters
    ----------
    model_y: estimator
        The estimator for fitting the response to the features. Must implement
        `fit` and `predict` methods.

    model_t: estimator
        The estimator for fitting the treatment to the features. Must implement
        `fit` and `predict` methods.

    featurizer: transformer, optional
    (default is :class:`PolynomialFeatures(degree=1, include_bias=True) <sklearn.preprocessing.PolynomialFeatures>`)
        The transformer used to featurize the raw features when fitting the final model.  Must implement
        a `fit_transform` method.

    linear_first_stages: bool
        Whether the first stage models are linear (in which case we will expand the features passed to
        `model_y` accordingly)

    discrete_treatment: bool, optional (default is ``False``)
        Whether the treatment values should be treated as categorical, rather than continuous, quantities

    n_splits: int, cross-validation generator or an iterable, optional
        Determines the cross-validation splitting strategy.
        Possible inputs for cv are:

        - None, to use the default 3-fold cross-validation,
        - integer, to specify the number of folds.
        - :term:`CV splitter`
        - An iterable yielding (train, test) splits as arrays of indices.

        For integer/None inputs, if the treatment is discrete
        :class:`~sklearn.model_selection.StratifiedKFold` is used, else,
        :class:`~sklearn.model_selection.KFold` is used
        (with a random shuffle in either case).

    random_state: int, :class:`~numpy.random.mtrand.RandomState` instance or None, optional (default=None)
        If int, random_state is the seed used by the random number generator;
        If :class:`~numpy.random.mtrand.RandomState` instance, random_state is the random number generator;
        If None, the random number generator is the :class:`~numpy.random.mtrand.RandomState` instance used
        by `np.random`.

    """

    def __init__(self,
                 model_y=LassoCV(), model_t=LassoCV(),
                 model_final=StatsModelsLinearRegression(fit_intercept=False),
                 featurizer=PolynomialFeatures(degree=1, include_bias=True),
                 linear_first_stages=True,
                 discrete_treatment=False,
                 n_splits=2,
                 random_state=None):
        super().__init__(model_y=model_y,
                         model_t=model_t,
                         model_final=model_final,
                         featurizer=featurizer,
                         linear_first_stages=linear_first_stages,
                         discrete_treatment=discrete_treatment,
                         n_splits=n_splits,
                         random_state=random_state)

    # override only so that we can update the docstring to indicate support for `StatsModelsInference`
    def fit(self, Y, T, X=None, W=None, sample_weight=None, sample_var=None, inference=None):
        """
        Estimate the counterfactual model from data, i.e. estimates functions τ(·,·,·), ∂τ(·,·).

        Parameters
        ----------
        Y: (n × d_y) matrix or vector of length n
            Outcomes for each sample
        T: (n × dₜ) matrix or vector of length n
            Treatments for each sample
        X: optional (n × dₓ) matrix
            Features for each sample
        W: optional (n × d_w) matrix
            Controls for each sample
        sample_weight: optional (n,) vector
            Weights for each row
        inference: string, `Inference` instance, or None
            Method for performing inference.  This estimator supports 'bootstrap'
            (or an instance of :class:`.BootstrapInference`) and 'statsmodels'
            (or an instance of :class:`.StatsModelsInference`)

        Returns
        -------
        self
        """
        return super().fit(Y, T, X=X, W=W, sample_weight=sample_weight, sample_var=sample_var, inference=inference)

    @property
    def statsmodels(self):
        return self._model_final._model


class SparseLinearDMLCateEstimator(DMLCateEstimator):
    """
    A specialized version of the Double ML estimator for the sparse linear case.

    Specifically, this estimator can be used when the controls are high-dimensional
    and the coefficients of the nuisance functions are sparse.

    Parameters
    ----------
    model_y: estimator
        The estimator for fitting the response to the features. Must implement
        `fit` and `predict` methods.

    model_t: estimator
        The estimator for fitting the treatment to the features. Must implement
        `fit` and `predict` methods, and must be a linear model for correctness.

    model_final: estimator, optional (default is :class:`LassoCV(fit_intercept=False)  <sklearn.linear_model.LassoCV>`)
        The estimator for fitting the response residuals to the treatment residuals. Must implement
        `fit` and `predict` methods, and must be a linear model with no intercept for correctness.

    featurizer: transformer, optional
    (default is :class:`PolynomialFeatures(degree=1, include_bias=True) <sklearn.preprocessing.PolynomialFeatures>`)
        The transformer used to featurize the raw features when fitting the final model.  Must implement
        a `fit_transform` method.

    linear_first_stages: bool
        Whether the first stage models are linear (in which case we will expand the features passed to
        `model_y` accordingly)

    discrete_treatment: bool, optional (default is ``False``)
        Whether the treatment values should be treated as categorical, rather than continuous, quantities

    n_splits: int, cross-validation generator or an iterable, optional
        Determines the cross-validation splitting strategy.
        Possible inputs for cv are:

        - None, to use the default 3-fold cross-validation,
        - integer, to specify the number of folds.
        - :term:`CV splitter`
        - An iterable yielding (train, test) splits as arrays of indices.

        For integer/None inputs, if the treatment is discrete
        :class:`~sklearn.model_selection.StratifiedKFold` is used, else,
        :class:`~sklearn.model_selection.KFold` is used
        (with a random shuffle in either case).

    random_state: int, :class:`~numpy.random.mtrand.RandomState` instance or None, optional (default=None)
        If int, random_state is the seed used by the random number generator;
        If :class:`~numpy.random.mtrand.RandomState` instance, random_state is the random number generator;
        If None, the random number generator is the :class:`~numpy.random.mtrand.RandomState` instance used
        by `np.random`.
    """

    def __init__(self,
                 model_y=LassoCV(), model_t=LassoCV(), model_final=LassoCVWrapper(fit_intercept=False),
                 featurizer=PolynomialFeatures(degree=1, include_bias=True),
                 linear_first_stages=True,
                 discrete_treatment=False,
                 n_splits=2,
                 random_state=None):
        super().__init__(model_y=model_y,
                         model_t=model_t,
                         model_final=model_final,
                         featurizer=featurizer,
                         linear_first_stages=linear_first_stages,
                         discrete_treatment=discrete_treatment,
                         n_splits=n_splits,
                         random_state=random_state)


class KernelDMLCateEstimator(LinearDMLCateEstimator):
    """
    A specialized version of the linear Double ML Estimator that uses random fourier features.

    Parameters
    ----------
    model_y: estimator, optional (default is :class:`LassoCV() <sklearn.linear_model.LassoCV>`)
        The estimator for fitting the response to the features. Must implement
        `fit` and `predict` methods.

    model_t: estimator, optional (default is :class:`LassoCV() <sklearn.linear_model.LassoCV>`)
        The estimator for fitting the treatment to the features. Must implement
        `fit` and `predict` methods.

    dim: int, optional (default is 20)
        The number of random Fourier features to generate

    bw: float, optional (default is 1.0)
        The bandwidth of the Gaussian used to generate features

    discrete_treatment: bool, optional (default is ``False``)
        Whether the treatment values should be treated as categorical, rather than continuous, quantities

    n_splits: int, cross-validation generator or an iterable, optional
        Determines the cross-validation splitting strategy.
        Possible inputs for cv are:

        - None, to use the default 3-fold cross-validation,
        - integer, to specify the number of folds.
        - :term:`CV splitter`
        - An iterable yielding (train, test) splits as arrays of indices.

        For integer/None inputs, if the treatment is discrete
        :class:`~sklearn.model_selection.StratifiedKFold` is used, else,
        :class:`~sklearn.model_selection.KFold` is used
        (with a random shuffle in either case).


    random_state: int, :class:`~numpy.random.mtrand.RandomState` instance or None, optional (default=None)
        If int, random_state is the seed used by the random number generator;
        If :class:`~numpy.random.mtrand.RandomState` instance, random_state is the random number generator;
        If None, the random number generator is the :class:`~numpy.random.mtrand.RandomState` instance used
        by `np.random`.
    """

    def __init__(self, model_y=LassoCV(), model_t=LassoCV(),
                 dim=20, bw=1.0, discrete_treatment=False, n_splits=2, random_state=None):
        class RandomFeatures(TransformerMixin):
            def __init__(self, random_state):
                self._random_state = check_random_state(random_state)

            def fit(self, X):
                self.omegas = self._random_state.normal(0, 1 / bw, size=(shape(X)[1], dim))
                self.biases = self._random_state.uniform(0, 2 * np.pi, size=(1, dim))
                return self

            def transform(self, X):
                return np.sqrt(2 / dim) * np.cos(np.matmul(X, self.omegas) + self.biases)

        super().__init__(model_y=model_y, model_t=model_t,
                         featurizer=RandomFeatures(random_state),
                         discrete_treatment=discrete_treatment, n_splits=n_splits, random_state=random_state)
