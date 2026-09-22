"""Statistical calibration helpers used by propagation policy contracts."""

from __future__ import annotations

import math


def _validate_counts(failures: int, trials: int, alpha: float) -> None:
    if isinstance(failures, bool) or not isinstance(failures, int):
        raise TypeError("failures must be an integer")
    if isinstance(trials, bool) or not isinstance(trials, int):
        raise TypeError("trials must be an integer")
    if trials <= 0:
        raise ValueError("trials must be positive")
    if failures < 0 or failures > trials:
        raise ValueError("failures must lie in [0, trials]")
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)):
        raise TypeError("alpha must be a real number")
    if not math.isfinite(alpha) or not 0 < alpha < 1:
        raise ValueError("alpha must lie strictly between zero and one")


def _binomial_cdf(successes: int, trials: int, probability: float) -> float:
    """Evaluate ``P[X <= successes]`` stably without third-party packages."""

    if successes >= trials or probability <= 0:
        return 1.0
    if probability >= 1:
        return 0.0

    log_p = math.log(probability)
    log_q = math.log1p(-probability)
    terms = [
        (
            math.lgamma(trials + 1)
            - math.lgamma(index + 1)
            - math.lgamma(trials - index + 1)
            + index * log_p
            + (trials - index) * log_q
        )
        for index in range(successes + 1)
    ]
    largest = max(terms)
    if largest == -math.inf:
        return 0.0
    return min(1.0, math.exp(largest) * sum(math.exp(term - largest) for term in terms))


def _clopper_pearson_upper_fallback(
    failures: int,
    trials: int,
    alpha: float,
) -> float:
    """Invert the exact binomial CDF by global monotone bisection."""

    if failures == trials:
        return 1.0
    lower = 0.0
    upper = 1.0
    # CDF(failures; trials, p) decreases in p.  The upper endpoint solves
    # CDF == alpha.  Returning ``upper`` keeps floating-point error conservative.
    for _ in range(100):
        midpoint = (lower + upper) / 2
        if _binomial_cdf(failures, trials, midpoint) > alpha:
            lower = midpoint
        else:
            upper = midpoint
    return upper


def clopper_pearson_upper(
    failures: int,
    trials: int,
    alpha: float = 0.05,
    *,
    use_scipy: bool = True,
) -> float:
    """Return the one-sided ``1-alpha`` exact binomial upper bound.

    SciPy is optional.  If unavailable, this function inverts the binomial CDF
    directly; it never substitutes a Wilson or asymptotic interval.
    """

    _validate_counts(failures, trials, alpha)
    if failures == trials:
        return 1.0

    if use_scipy:
        try:
            from scipy.stats import beta  # type: ignore[import-not-found]

            value = float(beta.ppf(1 - alpha, failures + 1, trials - failures))
            if math.isfinite(value):
                return min(1.0, max(0.0, value))
        except (ImportError, ModuleNotFoundError):
            pass

    return _clopper_pearson_upper_fallback(failures, trials, alpha)

