# ml/

Model data and artifacts. Training code lives in the services that use the models
(`services/anomaly_detector/anomaly_detector/trainer.py`,
`services/traffic_predictor/traffic_predictor/forecaster.py`).

| Path | Contents |
|------|----------|
| `artifacts/` | Trained model files (`.joblib`). Git-ignored; regenerate with `POST /train` on the anomaly detector. |
| `datasets/` | `mojaloop_replay_28d.csv`, the committed 28-day training set (see [docs/DATA.md](../docs/DATA.md)). |

Every training run writes a row to the `model_runs` table (model name, version, row count,
duration, metrics, artifact path) so a model in production can always be traced back to
the data and code that produced it.
