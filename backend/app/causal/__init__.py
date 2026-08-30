"""Causal analysis over sealed experiment data.

Day 3 ships balance diagnostics only (`balance.py`). Effect estimation,
intervals, uplift, and the abstention gate land later and must not be
anticipated here.

Nothing in this package writes. Nothing in it reads ground truth: `truth_*`
fields belong to the simulator and never cross into analysis, or the estimate
would be measuring the answer rather than deriving it.
"""

from app.causal.balance import (
    BALANCE_COVARIATES,
    IMBALANCE_THRESHOLD_BPS,
    MISSING_LEVEL,
    BalanceError,
    BalanceReport,
    CovariateBalance,
    CovariateRow,
    LevelBalance,
    Smd,
    balance_report,
    categorical_balance,
    continuous_balance,
    covariate_rows,
    mean_smd_bps,
    proportion_smd_bps,
    report_for_experiment,
    split_stratum_key,
)

__all__ = [
    "BALANCE_COVARIATES",
    "IMBALANCE_THRESHOLD_BPS",
    "MISSING_LEVEL",
    "BalanceError",
    "BalanceReport",
    "CovariateBalance",
    "CovariateRow",
    "LevelBalance",
    "Smd",
    "balance_report",
    "categorical_balance",
    "continuous_balance",
    "covariate_rows",
    "mean_smd_bps",
    "proportion_smd_bps",
    "report_for_experiment",
    "split_stratum_key",
]
