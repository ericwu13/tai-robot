"""Trial-count penalty: Deflated Sharpe Ratio and CSCV PBO.

DSR is Bailey & López de Prado (2014): the probability that the true
Sharpe of the chosen configuration is above 0 after a penalty for the
number of configurations tried. PBO is Bailey, Borwein, López de Prado
& Zhu (2015): the fraction of combinatorially symmetric cross-validation
splits in which the in-sample best lands strictly below the
out-of-sample median.

Both are pure functions. No numpy. The Sharpe ratio is the mean of fold
PnL divided by its sample standard deviation (not annualized). Kurtosis
is non-excess (a normal sample is 3). The trial count that enters DSR
is the effective count ``N × (1 − ρ̄) + ρ̄``, where ρ̄ is the mean
pairwise Pearson correlation of the fold-PnL vectors.

CSCV uses S = 10 partitions when there are at least 10 folds, and S = 8
when there are 8 or 9. Fewer than 8 folds does not produce a PBO; the
caller treats that as a miss, not a pass.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Euler-Mascheroni constant. The expected-maximum Sharpe uses it as the
# weight between the two inverse-normal terms in Bailey & López de Prado.
_EULER = 0.5772156649015328606

# Acklam's inverse-normal approximation (relative error ~1e-9).
_PPF_A = (
    -3.969683028665376e+01,
    2.209460984245205e+02,
    -2.759285104469687e+02,
    1.383577518672690e+02,
    -3.066479806614716e+01,
    2.506628277459239e+00,
)
_PPF_B = (
    -5.447609879822406e+01,
    1.615858368580409e+02,
    -1.556989798598866e+02,
    6.680131188771972e+01,
    -1.328068155288572e+01,
)
_PPF_C = (
    -7.784894002430293e-03,
    -3.223964580411385e-01,
    -2.400758277161838e+00,
    -2.549732539343734e+00,
    4.374664141464968e+00,
    2.938163982698783e+00,
)
_PPF_D = (
    7.784695709041462e-03,
    3.224671290700398e-01,
    2.445134137142996e+00,
    3.754408661907416e+00,
)
_PPF_LOW = 0.02425
_PPF_HIGH = 1.0 - _PPF_LOW


@dataclass(frozen=True)
class TrialEvidence:
    """Measured trial-set penalty for one chosen configuration.

    ``dsr`` and ``pbo`` are None when they could not be computed. A
    missing number is not a pass. ``partitions`` is the CSCV S that was
    used, or None when the fold count could not support S = 8 or 10.
    """

    dsr: float | None
    pbo: float | None
    n_trials: int
    n_eff: float
    rho_bar: float
    partitions: int | None


def norm_cdf(x: float) -> float:
    """Standard normal CDF via ``math.erf``."""
    if not math.isfinite(x):
        return 0.0 if x < 0 else 1.0
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Standard normal inverse CDF (Acklam, one Halley step).

    ``p`` must be in (0, 1). The rational approximation alone is about
    1e-9; the refinement against ``math.erfc`` brings the central
    quantiles (0.975 in particular) inside 1e-12 of the true value.
    """
    if not math.isfinite(p) or p <= 0.0 or p >= 1.0:
        raise ValueError(f"norm_ppf probability out of range: {p}")
    if p < _PPF_LOW:
        q = math.sqrt(-2.0 * math.log(p))
        num = (((((_PPF_C[0] * q + _PPF_C[1]) * q + _PPF_C[2]) * q
                 + _PPF_C[3]) * q + _PPF_C[4]) * q + _PPF_C[5])
        den = ((((_PPF_D[0] * q + _PPF_D[1]) * q + _PPF_D[2]) * q
                + _PPF_D[3]) * q + 1.0)
        x = num / den
    elif p > _PPF_HIGH:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        num = (((((_PPF_C[0] * q + _PPF_C[1]) * q + _PPF_C[2]) * q
                 + _PPF_C[3]) * q + _PPF_C[4]) * q + _PPF_C[5])
        den = ((((_PPF_D[0] * q + _PPF_D[1]) * q + _PPF_D[2]) * q
                + _PPF_D[3]) * q + 1.0)
        x = -(num / den)
    else:
        q = p - 0.5
        r = q * q
        num = (((((_PPF_A[0] * r + _PPF_A[1]) * r + _PPF_A[2]) * r
                 + _PPF_A[3]) * r + _PPF_A[4]) * r + _PPF_A[5]) * q
        den = (((((_PPF_B[0] * r + _PPF_B[1]) * r + _PPF_B[2]) * r
                 + _PPF_B[3]) * r + _PPF_B[4]) * r + 1.0)
        x = num / den
    # One Halley step: e = Φ(x) − p, then correct by the mills ratio.
    e = 0.5 * math.erfc(-x / math.sqrt(2.0)) - p
    u = e * math.sqrt(2.0 * math.pi) * math.exp(0.5 * x * x)
    return x - u / (1.0 + x * u / 2.0)


def effective_trial_count(n: int, rho_bar: float) -> float:
    """``N_eff = N × (1 − ρ̄) + ρ̄``.

    Uncorrelated trials keep N. Identical trials collapse to 1. A
    non-finite correlation is treated as 0 (independent), which is the
    harsher penalty. The result is not rounded.
    """
    n = int(n)
    if n <= 0:
        return 0.0
    if n == 1:
        return 1.0
    if not math.isfinite(rho_bar):
        rho = 0.0
    else:
        rho = max(-1.0, min(1.0, float(rho_bar)))
    return n * (1.0 - rho) + rho


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def sharpe_ratio(returns: list[float]) -> float | None:
    """Mean / sample standard deviation. None when undefined.

    Fewer than 2 observations, a non-finite value, or a zero sample
    standard deviation has no Sharpe. A constant series is not given
    an infinite Sharpe.
    """
    if len(returns) < 2:
        return None
    try:
        xs = [float(x) for x in returns]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(x) for x in xs):
        return None
    mu = _mean(xs)
    var = sum((x - mu) ** 2 for x in xs) / (len(xs) - 1)
    if var <= 0.0:
        return None
    return mu / math.sqrt(var)


def sample_moments(returns: list[float]) -> tuple[float, float] | None:
    """Population skewness and non-excess kurtosis (normal kurtosis = 3).

    None when the second moment is 0 or any value is non-finite. The
    divisor is N, matching the usual bias-corrected-off moments used
    with the Mertens standard error of the Sharpe ratio.
    """
    if len(returns) < 2:
        return None
    try:
        xs = [float(x) for x in returns]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(x) for x in xs):
        return None
    mu = _mean(xs)
    devs = [x - mu for x in xs]
    m2 = sum(d * d for d in devs) / len(xs)
    if m2 <= 0.0:
        return None
    m3 = sum(d ** 3 for d in devs) / len(xs)
    m4 = sum(d ** 4 for d in devs) / len(xs)
    skew = m3 / (m2 ** 1.5)
    kurtosis = m4 / (m2 * m2)
    if not math.isfinite(skew) or not math.isfinite(kurtosis):
        return None
    return skew, kurtosis


def expected_maximum_sharpe(n_trials: float, sharpe_std: float) -> float:
    """Expected maximum Sharpe under a null of zero, given ``n_trials``.

    Bailey & López de Prado (2014), "The Deflated Sharpe Ratio":

        SR0 = σ_SR * ((1 - γ) Z^{-1}(1 - 1/N) + γ Z^{-1}(1 - 1/(N*e)))

    ``N`` is the effective trial count. ``σ_SR`` (``sharpe_std``) is the
    cross-trial standard deviation of the Sharpe ratios, not the
    estimation error of one Sharpe. ``γ`` is the Euler-Mascheroni
    constant. One trial, or a non-positive dispersion, has no selection
    premium, so SR0 is 0. Forcing SR0 to 0 for N > 1 drops the penalty
    and is not this formula.
    """
    if not math.isfinite(n_trials) or n_trials <= 1.0:
        return 0.0
    if not math.isfinite(sharpe_std) or sharpe_std <= 0.0:
        return 0.0
    z1 = norm_ppf(1.0 - 1.0 / n_trials)
    z2 = norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    return sharpe_std * ((1.0 - _EULER) * z1 + _EULER * z2)


def deflated_sharpe_ratio(
        observed_sharpe: float,
        sharpe_std: float,
        n_trials: float,
        n_obs: int,
        skew: float,
        kurtosis: float) -> float | None:
    """Probability that the true Sharpe exceeds 0 after the trial penalty.

    Bailey & López de Prado (2014), "The Deflated Sharpe Ratio":

        DSR = Z[ (SR - SR0) / SE(SR) ]
        SE(SR) = sqrt( (1 - γ3*SR + ((γ4 - 1)/4)*SR^2) / (T - 1) )

    ``SR0`` is ``expected_maximum_sharpe`` (0 only when N ≤ 1 or the
    cross-trial dispersion is 0). ``γ3`` is skewness and ``γ4`` is
    non-excess kurtosis. None when the estimation variance is not
    positive or an input is non-finite. The caller treats None as a miss.
    """
    if n_obs < 2:
        return None
    if not all(math.isfinite(v) for v in (
            observed_sharpe, sharpe_std, n_trials, skew, kurtosis)):
        return None
    if sharpe_std < 0.0:
        return None
    sr0 = expected_maximum_sharpe(n_trials, sharpe_std)
    var = (
        1.0
        - skew * observed_sharpe
        + ((kurtosis - 1.0) / 4.0) * observed_sharpe * observed_sharpe
    ) / (n_obs - 1)
    if not math.isfinite(var) or var <= 0.0:
        return None
    z = (observed_sharpe - sr0) / math.sqrt(var)
    if not math.isfinite(z):
        return None
    return norm_cdf(z)


def _pearson(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 2 or len(b) != n:
        return None
    if not all(math.isfinite(x) for x in a + b):
        return None
    ma = _mean(a)
    mb = _mean(b)
    da = [x - ma for x in a]
    db = [y - mb for y in b]
    va = sum(x * x for x in da)
    vb = sum(y * y for y in db)
    if va <= 0.0 or vb <= 0.0:
        return None
    corr = sum(x * y for x, y in zip(da, db)) / math.sqrt(va * vb)
    if not math.isfinite(corr):
        return None
    return max(-1.0, min(1.0, corr))


def mean_pairwise_correlation(columns: list[list[float]]) -> float:
    """Mean Pearson correlation of fold-PnL columns.

    Pairs with an undefined correlation (constant column, length
    mismatch, non-finite) are skipped. No defined pair → 0, so the
    effective trial count stays at N.
    """
    found: list[float] = []
    for i in range(len(columns)):
        for j in range(i + 1, len(columns)):
            corr = _pearson(columns[i], columns[j])
            if corr is not None:
                found.append(corr)
    if not found:
        return 0.0
    return sum(found) / len(found)


def _sample_std(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mu = _mean(values)
    var = sum((x - mu) ** 2 for x in values) / (n - 1)
    if var <= 0.0:
        return 0.0
    return math.sqrt(var)


def choose_partitions(n_folds: int) -> int | None:
    """CSCV partition count: 10 when ``n_folds >= 10``, else 8 when
    ``n_folds >= 8``. None below 8 — PBO is then not a pass.
    """
    n = int(n_folds)
    if n >= 10:
        return 10
    if n >= 8:
        return 8
    return None


def _partition_blocks(n_rows: int, s: int) -> list[list[int]]:
    """Contiguous blocks. The remainder is given to the earliest blocks."""
    base, extra = divmod(n_rows, s)
    blocks: list[list[int]] = []
    start = 0
    for i in range(s):
        size = base + (1 if i < extra else 0)
        blocks.append(list(range(start, start + size)))
        start += size
    return blocks


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def cscv_pbo(matrix: list[list[float]], partitions: int) -> float | None:
    """Fraction of CSCV splits whose in-sample best is overfit out of sample.

    ``matrix[t][j]`` is configuration ``j`` in fold ``t``. ``partitions``
    must be an even integer >= 2 and must not exceed the row count.
    Ties for the in-sample best take the lowest column index. Overfit
    means the in-sample best's out-of-sample score is less than or equal
    to the OOS median (a tie with the median counts). None when the
    matrix cannot be split.
    """
    if partitions < 2 or partitions % 2 != 0:
        return None
    if not matrix:
        return None
    n_rows = len(matrix)
    n_cols = len(matrix[0])
    if n_cols < 1 or partitions > n_rows:
        return None
    columns: list[list[float]] = []
    for j in range(n_cols):
        col: list[float] = []
        for row in matrix:
            if len(row) != n_cols:
                return None
            try:
                value = float(row[j])
            except (TypeError, ValueError):
                return None
            if not math.isfinite(value):
                return None
            col.append(value)
        columns.append(col)
    blocks = _partition_blocks(n_rows, partitions)
    half = partitions // 2
    # Combinations of block indexes. Both a split and its complement are
    # included: which half is in-sample is the thing CSCV varies.
    from itertools import combinations
    below = 0
    total = 0
    for chosen in combinations(range(partitions), half):
        is_rows: list[int] = []
        chosen_set = set(chosen)
        oos_rows: list[int] = []
        for b, block in enumerate(blocks):
            if b in chosen_set:
                is_rows.extend(block)
            else:
                oos_rows.extend(block)
        is_score = [
            sum(columns[j][t] for t in is_rows) for j in range(n_cols)
        ]
        best = max(range(n_cols), key=lambda j: (is_score[j], -j))
        oos_score = [
            sum(columns[j][t] for t in oos_rows) for j in range(n_cols)
        ]
        if oos_score[best] <= _median(oos_score):
            below += 1
        total += 1
    if total == 0:
        return None
    return below / total


def probability_of_backtest_overfitting(
        matrix: list[list[float]]) -> tuple[float | None, int | None]:
    """``(pbo, partitions)`` using S = 8 or 10. ``(None, None)`` below 8 folds."""
    partitions = choose_partitions(len(matrix))
    if partitions is None:
        return None, None
    return cscv_pbo(matrix, partitions), partitions


def score_configuration(
        fold_pnls: list[float],
        *,
        n_eff: float,
        sharpe_std: float) -> float | None:
    """DSR of one configuration's fold-PnL vector."""
    sr = sharpe_ratio(fold_pnls)
    moments = sample_moments(fold_pnls)
    if sr is None or moments is None:
        return None
    skew, kurtosis = moments
    return deflated_sharpe_ratio(
        sr, sharpe_std, n_eff, len(fold_pnls), skew, kurtosis)


def trial_sharpe_std(sharpes: list[float]) -> float:
    """Cross-sectional sample standard deviation of finite trial Sharpes."""
    finite = [s for s in sharpes if s is not None and math.isfinite(s)]
    return _sample_std(finite)
