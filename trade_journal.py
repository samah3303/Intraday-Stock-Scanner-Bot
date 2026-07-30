"""
AlphaQuant AI — Trade Outcome Journal & Continuous Learning Pipeline
=====================================================================
Logs real trade outcomes to disk (trade_outcomes.json) and handles periodic
retraining of the ML classifier when sufficient historical samples exist.
"""

import os
import json
import logging
import pandas as pd
from typing import Dict, Any, List

logger = logging.getLogger("TradeJournal")

JOURNAL_FILE = os.path.join(os.path.dirname(__file__), "trade_outcomes.json")


def log_trade_outcome(symbol: str, features_dict: Dict[str, Any], outcome: Dict[str, Any]) -> None:
    """
    Appends a single completed trade record as a JSON line to trade_outcomes.json.
    
    Args:
        symbol: Ticker symbol (e.g., "RELIANCE")
        features_dict: Extracted feature vector (rvol, gap_pct, orderbook_imbalance, etc.)
        outcome: Trade outcome dictionary (status, pnl_r, exit_price, etc.)
    """
    record = {
        "timestamp": pd.Timestamp.now().isoformat(),
        "symbol": symbol,
        "features": features_dict,
        "outcome": outcome,
        "target_label": 1 if outcome.get("pnl_r", 0.0) > 0 else 0
    }
    
    try:
        with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        logger.info("Logged trade outcome for %s to %s (P/L: %.2f R)", symbol, JOURNAL_FILE, outcome.get("pnl_r", 0.0))
    except Exception as exc:
        logger.error("Failed to log trade outcome for %s: %s", symbol, exc)


def load_trade_history() -> List[Dict[str, Any]]:
    """Reads all past trade outcome records from trade_outcomes.json."""
    if not os.path.exists(JOURNAL_FILE):
        return []
    
    records = []
    try:
        with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        logger.info("Loaded %d trade outcome record(s) from journal.", len(records))
    except Exception as exc:
        logger.error("Error reading trade_outcomes.json: %s", exc)
    return records


def retrain_from_history(classifier=None, pipeline=None) -> bool:
    """
    Merges real recorded trade outcomes with synthetic baseline data, retrains,
    recalibrates, and persists the ML classifier model to disk.
    
    Only retrains if >= 20 real trade records exist in the journal.
    """
    history = load_trade_history()
    if len(history) < 20:
        logger.info("Skipping retraining: Only %d real trade(s) logged (minimum 20 required).", len(history))
        return False

    logger.info("Retraining ML Classifier on %d real trade outcomes...", len(history))
    
    # Build DataFrame from real history
    real_rows = []
    real_targets = []
    for item in history:
        feats = item.get("features", {})
        if feats:
            real_rows.append(feats)
            real_targets.append(item.get("target_label", 0))

    df_real = pd.DataFrame(real_rows)
    y_real = pd.Series(real_targets)

    # Instantiate classifier if not provided
    if classifier is None:
        from ml_engine import OELSetupClassifier
        classifier = OELSetupClassifier(model_type="xgboost")

    # Generate synthetic baseline & merge
    df_synth, y_synth = classifier.generate_synthetic_training_data(n_samples=3000)
    
    # Align feature columns
    common_cols = [c for c in df_synth.columns if c in df_real.columns]
    if not common_cols:
        logger.error("Feature mismatch between real outcomes and synthetic baseline.")
        return False

    df_combined = pd.concat([df_synth[common_cols], df_real[common_cols]], ignore_index=True)
    y_combined = pd.concat([y_synth, y_real], ignore_index=True)

    # Train & Calibrate
    classifier.fit(df_combined, y_combined)

    # Save trained model to disk
    model_path = os.path.join(os.path.dirname(__file__), "oel_model.json")
    classifier.save_model(model_path)
    logger.info("Retraining complete. Calibrated model updated at %s", model_path)
    return True
