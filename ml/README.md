# ml/

Model development area — separate from the services that *serve* the models.

| Path | Contents |
|------|----------|
| `artifacts/` | Trained model files (`.joblib`) and scalers. Git-ignored; regenerate by training. |
| `datasets/` | Exported training data snapshots. Git-ignored. |
| `notebooks/` | Exploration and evaluation notebooks. |
| `training/` | Reproducible training scripts invoked by the services and by CI. |

Every training run writes a row to the `model_runs` table (model name, version, row count,
duration, metrics, artifact path) so a model in production can always be traced back to
the data and code that produced it.
