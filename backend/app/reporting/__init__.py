"""Report generation.

`evaluation.py` is the single place in the application permitted to read the
`truth_*` columns, because scoring the estimator against the answer key is what
an evaluation report is for. Nothing else in this package may reach them, and a
guard over the whole application package enforces it.
"""
