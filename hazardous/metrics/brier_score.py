import warnings

import numpy as np

from .._ipcw import IPCWEstimator
from ..utils import check_event_of_interest, check_y_survival


class BrierScoreComputer:
    """Time-dependent Brier score with adjusted for censoring.

    This class factorizes the computation of the Brier score for single-event
    or any event survival functions and cause-specific cumulative incidence
    functions.

    It leverages the Inverse Probability of Censoring Weighting (IPCW) scheme
    using a Kaplan-Meier of the censoring distribution to weight the terms.

    Parameters
    ----------
    y_train : np.array, dictionnary or dataframe
        The target, consisting in the 'event' and 'duration' columns. This is
        used to fit the IPCW estimator.

    event_of_interest : int or "any", default="any"
        The event to consider in a competing events setting. "any" indicates that
        all events except the censoring marker 0 are considered as a single
        event. In single event settings, "any" and 1 are equivalent.

    """

    def __init__(
        self,
        y_train,
        event_of_interest="any",
    ):
        self.y_train = y_train
        self.event_train, self.duration_train = check_y_survival(y_train)
        self.event_ids_ = np.unique(self.event_train)
        self.any_event_train = self.event_train > 0
        self.event_of_interest = event_of_interest

        # Estimate the censoring distribution from the training set
        # using Kaplan-Meier.
        self.ipcw_est = IPCWEstimator().fit(
            dict(
                event=self.any_event_train,
                duration=self.duration_train,
            )
        )

        # Precompute the censoring probabilities at the time of the events on the
        # training set:
        self.ipcw_train = self.ipcw_est.predict(self.duration_train)

    def brier_score_survival(self, y_true, y_pred, times):
        """Time-dependent Brier score of a survival function estimate.

        Compute the time-dependent Brier score value for each individual and
        each time point in `times` and then average over individuals.

        This estimate is adjusted for censoring by leveraging the Inverse
        Probability of Censoring Weighting (IPCW) scheme.

        Parameters
        ----------
        y_true : record-array, dict or dataframe of shape (n_samples, 2)
            The ground truth, consisting in the 'event' and 'duration' columns.
            In a survival setting, we expect the event to be a binary
            indicator: 1 for the event of interest and 0 for censoring.
            Alternatively, all competing event types should be collapsed by
            setting event_of_interest="any".

        y_pred : array-like of shape (n_samples, n_times)
            Survival probability estimates predicted at ``times``. In the
            binary event settings, this is 1 - incidence_probability.

        times : array-like of shape (n_times)
            Times to estimate the survival probability and to compute the Brier
            Score.

        Returns
        -------
        brier_score : np.ndarray of shape (n_times)
            Time-dependent Brier scores averaged over the individuals.

        """
        if (self.event_ids_ > 0).sum() > 1 and self.event_of_interest != "any":
            warnings.warn(
                "Computing the survival Brier score only makes "
                "sense with a binary event indicator or when setting "
                "event_of_interest='any'. "
                "Instead this model is evaluated on data with event ids "
                f"{self.event_ids_.tolist()} and with "
                f"event_of_interest={self.event_of_interest}."
            )
        return self.brier_score_incidence(y_true, 1 - y_pred, times)

    def brier_score_incidence(self, y_true, y_pred, times):
        """Brier score for the cause-specific cumulative incidence function.

        Compute the Brier score values with IPCW adjustment for censoring for
        each cumulative incidence estimate for the event of interest and each
        requested time point and return the time-dependent Brier score averaged
        over individuals.

        Parameters
        ----------
        y_true : record-array, dictionnary or dataframe of shape (n_samples, 2)
            The ground truth, consisting in the 'event' and 'duration' columns.

        y_pred : array-like of shape (n_samples, n_times)
            Cause-specific cumulative incidence estimates predicted at
            ``times`` for the event of interest. In the single event type
            settings, or when event_of_interest == "any", this is 1 -
            survival_probability.

        times : array-like of shape (n_times)
            Times to estimate the survival probability and to compute the Brier
            score.

        Returns
        -------
        brier_score_incidence : np.ndarray
            Average value of the time-dependent Brier scores computed at time
            locations specified in the ``times`` argument.
        """
        event_true, duration_true = check_y_survival(y_true)
        check_event_of_interest(self.event_of_interest)

        if self.event_of_interest == "any":
            if y_true is self.y_train:
                event_true = self.any_event_train
            else:
                event_true = event_true > 0

        if y_pred.shape[1] != times.shape[0]:
            raise ValueError(
                f"'times' length ({times.shape[0]}) "
                f"must be equal to y_pred.shape[1] ({y_pred.shape[1]})."
            )

        n_samples = event_true.shape[0]
        n_time_steps = times.shape[0]
        brier_scores = np.empty(
            shape=(n_samples, n_time_steps),
            dtype=np.float64,
        )
        ipcw_y = self.ipcw_est.predict(duration_true)
        for t_idx, t in enumerate(times):
            y_true_binary, weights = self._ibs_components(
                event=event_true,
                duration=duration_true,
                times=np.full(shape=n_samples, fill_value=t),
                ipcw_y=ipcw_y,
            )
            squared_error = (y_true_binary - y_pred[:, t_idx]) ** 2
            brier_scores[:, t_idx] = weights * squared_error

        return brier_scores.mean(axis=0)

    def _ibs_components(self, event, duration, times, ipcw_y):
        if self.event_of_interest == "any":
            # y should already be provided as binary indicator
            k = 1
        else:
            k = self.event_of_interest

        # Specify the binary classification target for each record in y and
        # a reference time horizon:
        #
        # - 1 when event of interest was observed before the reference time
        #   horizon,
        #
        # - 0 otherwise: any other event happening at any time, censored record
        #   or event of interest happening after the reference time horizon.
        #
        #   Note: censored events only contribute (as negative target) when
        #   their duration is larger than the reference target horizon.
        #   Otherwise, they are discarded by setting their weight to 0 in the
        #   following.

        y_binary = np.zeros(event.shape[0], dtype=np.int32)
        y_binary[(event == k) & (duration <= times)] = 1

        # Compute the weights for each term contributing to the Brier score
        # at the specified time horizons.
        #
        # - error of a prediction for a time horizon before the occurence of an
        #   event (either censored or uncensored) is weighted by the inverse
        #   probability of censoring at that time horizon.
        #
        # - error of a prediction for a time horizon after the any observed event
        #   is weighted by inverse censoring probability at the actual time
        #   of the observed event.
        #
        # - "error" of a prediction for a time horizon after a censored event has
        #   0 weight and do not contribute to the Brier score computation.

        # Estimate the probability of censoring at current time point t.
        ipcw_t = self.ipcw_est.predict(times)
        before = times < duration
        weights = np.where(before, ipcw_t, 0)

        after_any_observed_event = (event > 0) & (duration <= times)
        weights = np.where(after_any_observed_event, ipcw_y, weights)

        return y_binary, weights


def brier_score_survival(
    y_train,
    y_test,
    y_pred,
    times,
):
    """Compute the Brier score.

    .. math::

        \\mathrm{BS}(t) = \\frac{1}{n} \\sum_{i=1}^n \\mathbb{I}
        (y_i \\leq t \\land \\delta_i = 1)
        \\frac{(0 - \\hat{S}(t | \\mathbf{x}_i))^2}{\\hat{G}(y_i)} +
        \\mathbb{I}(y_i > t)
        \\frac{(1 - \\hat{S}(t | \\mathbf{x}_i))^2}{\\hat{G}(t)} ,

    where :math:`\\hat{S}(t | \\mathbf{x})` is the predicted probability of
    surviving up to time point :math:`t` for a feature vector :math:`\\mathbf{x}`,
    and :math:`\\hat{G}(t)` is the probability of remaining uncensored at time
    :math:`t`, estimated on the training set by the Kaplan-Meier estimator on the
    negation of the binary any-event indicator.

    Parameters
    ----------
    y_train : record-array, dictionnary or dataframe of shape (n_samples, 2)
        The target, consisting in the 'event' and 'duration' columns. If the
        'event' column holds more than 1 event types, they are automatically
        collapsed to a single event type to compute the Brier score of the
        "any-event" survival function estimate.
        This is only used to estimate the IPCW values to adjust for censoring in
        the evaluation data.

    y_test : record-array, dictionnary or dataframe of shape (n_samples, 2)
        The ground truth, consisting in the 'event' and 'duration' columns.
        The same remark applies as for ``y_train`` with respect to the 'event'
        column.

    y_pred : array-like of shape (n_samples, n_times)
        Survival probability estimates predicted at ``times``.

    times : array-like of shape (n_times)
        Times at which the survival probability ``y_pred`` has been estimated
        and for which we compute the Brier score.

    Returns
    -------
    times : np.ndarray of shape (n_times)
        No-op, this is the same as the input.

    brier_score : np.ndarray of shape (n_times)
    """
    computer = BrierScoreComputer(
        y_train,
        event_of_interest="any",
    )
    return times, computer.brier_score_survival(y_test, y_pred, times)


def integrated_brier_score_survival(
    y_train,
    y_test,
    y_pred,
    times,
):
    """Compute the Brier score integrated over the observed time range.

    .. math::

        \\mathrm{IBS}(t) = \\frac{1}{t_{max} - t_{min}} \\int^{t_{max}}_{t_{min}}
        \\mathrm{BS}(u) du

    Parameters
    ----------
    y_train : record-array, dictionnary or dataframe of shape (n_samples, 2)
        The target, consisting in the ``"event"`` and ``"duration"`` columns.
        This is used to fit the IPCW estimator.

    y_test : record-array, dictionnary or dataframe of shape (n_samples, 2)
        The ground truth, consisting in the ``"event"`` and ``"duration"`` columns.

    y_pred : array-like of shape (n_samples, n_times)
        Survival probability estimates predicted at ``times``.

    times : array-like of shape (n_times)
        Times at which the survival probabilities ``y_pred`` has been estimated
        and for which we compute the Brier score.

    Returns
    -------
    ibs : float
    """
    times, brier_scores = brier_score_survival(
        y_train,
        y_test,
        y_pred,
        times,
    )
    return np.trapz(brier_scores, times) / (times[-1] - times[0])


def brier_score_incidence(
    y_train,
    y_test,
    y_pred,
    times,
    event_of_interest="any",
):
    """Compute the time dependent Brier score for the kth cause of failure.

    .. math::

        \\mathrm{BS}_k(t) = \\frac{1}{n} \\sum_{i=1}^n \\hat{\\omega}_i(t)
        (\\mathbb{I}(t_i \\leq t, \\delta_i = k) - \\hat{F}_k(t|\\mathbf{x}_i))^2

    where :math:`\\hat{F}_k(t | \\mathbf{x}_i)` is the predicted probability of
    incidence of the kth event up to time point :math:`t`
    for a feature vector :math:`\\mathbf{x}_i`,
    and

    .. math::

        \\hat{\\omega}_i(t)=\\mathbb{I}(t_i \\leq t, \\delta_i \\neq 0)/\\hat{G}(t_i)
        + \mathbb{I}(t_i > t)/\\hat{G}(t)

    are weigths based on the Kaplan-Meier estimate of the censoring
    distribution :math:`\\hat{G}(t)`.

    Parameters
    ----------
    y_train : record-array, dictionnary or dataframe of shape (n_samples, 2)
        The target, consisting in the 'event' and 'duration' columns.
        This is used to fit the IPCW estimator.

    y_test : record-array, dictionnary or dataframe of shape (n_samples, 2)
        The ground truth, consisting in the 'event' and 'duration' columns.

    y_pred : array-like of shape (n_samples, n_times)
        Incidence probability estimates predicted at ``times``.
        In the binary event settings, this is 1 - survival_probability.

    times : array-like of shape (n_times)
        Times at which the survival probability ``y_pred`` has been estimated
        and for which we compute the Brier score.

    event_of_interest : int or "any", default="any"
        The event to consider in competing events setting.
        "any" indicates that all events except the censoring 0 are
        considered as a single event.
        In single event settings, "any" and 1 are equivalent.

    Returns
    -------
    times : np.ndarray of shape (n_times)
        No-op, this is the same as the input.

    brier_score : np.ndarray of shape (n_times)

    References
    ----------

    [1] M. Kretowska, "Tree-based models for survival data with competing risks",
        Computer Methods and Programs in Biomedicine 159 (2018) 185-198.
    """
    # XXX: make times an optional kwarg to be compatible with
    # sksurv.metrics.brier_score?
    # XXX: 'times' must match the times of y_pred,
    # but we have no way to check that.
    # In this sense, 'y_pred[:, t_idx]' is incorrect when 'times'
    # is not the time used during the prediction.
    computer = BrierScoreComputer(
        y_train,
        event_of_interest=event_of_interest,
    )
    return times, computer.brier_score_incidence(y_test, y_pred, times)


def integrated_brier_score_incidence(
    y_train,
    y_test,
    y_pred,
    times,
    event_of_interest="any",
):
    """Compute the Integrated Brier score Incidence for the kth cause of failure.

    .. math::

        \\mathrm{IBS}_k(t) = \\frac{1}{t_{max} - t_{min}} \\int^{t_{max}}_{t_{min}}
        \\mathrm{BS}_k(u) du

    Parameters
    ----------
    y_train : record-array, dictionnary or dataframe of shape (n_samples, 2)
        The target, consisting in the ``"event"`` and ``"duration"`` columns.
        This is used to fit the IPCW estimator.

    y_test : record-array, dictionnary or dataframe of shape (n_samples, 2)
        The ground truth, consisting in the ``"event"`` and ``"duration"`` columns.

    y_pred : array-like of shape (n_samples, n_times)
        Incidence probability estimates predicted at ``times``.
        In the binary event settings, this is 1 - survival_probability.

    times : array-like of shape (n_times)
        Times at which the survival probabilities ``y_pred`` has been estimated
        and for which we compute the Brier score.

    event_of_interest : int or "any", default="any"
        The event to consider in competing events setting.
        ``"any"`` indicates that all events except the censoring ``0`` are
        considered as a single event.
        In single event settings, ``"any"`` and ``1`` are equivalent.

    Returns
    -------
    ibs : float

    References
    ----------

    [1] M. Kretowska, "Tree-based models for survival data with competing risks",
        Computer Methods and Programs in Biomedicine 159 (2018) 185-198.
    """
    times, brier_scores = brier_score_incidence(
        y_train,
        y_test,
        y_pred,
        times,
        event_of_interest=event_of_interest,
    )
    return np.trapz(brier_scores, times) / (times[-1] - times[0])
