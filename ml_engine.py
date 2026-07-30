"""
ML-Augmented Open=Low (OEL) Trading Pipeline
===================================================
High-performance quantitative feature engineering, LightGBM/XGBoost classification,
and dynamic ATR-based risk management engine for 9:30 AM IST intraday breakout strategy.
"""

from dataclasses import dataclass
import logging
import os
from typing import Dict, Any, List, Optional, Tuple
import numpy as np
import pandas as pd
import joblib
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import IsolationForest

# Configure Module Logger
logger = logging.getLogger("OEL_ML_Engine")
logger.setLevel(logging.INFO)

# Optional Machine Learning Imports with Graceful Fallback
try:
    import xgboost as xgb
    XGBOOST_AVAILABLE = True
except ImportError:
    XGBOOST_AVAILABLE = False
    logger.warning("XGBoost library not found. Install via 'pip install xgboost' for ML inference.")

try:
    import lightgbm as lgb
    LIGHTGBM_AVAILABLE = True
except ImportError:
    LIGHTGBM_AVAILABLE = False


# ===========================================================================
# DATACLASS DEFINITIONS
# ===========================================================================

@dataclass(frozen=True)
class TradeParams:
    """Dataclass holding execution-ready risk & position parameters."""
    symbol: str
    entry_price: float
    stop_loss: float
    target_1: float
    target_2: float
    risk_reward_ratio: float
    position_size: int
    capital_allocated: float
    atr: float
    model_confidence: float


# ===========================================================================
# 1. FEATURE ENGINEERING MODULE
# ===========================================================================

class FeatureExtractor:
    """
    Real-time Feature Engineering Engine.
    Vectorized computation of morning intraday breakout features (9:15 - 9:30 AM IST).
    """

    @staticmethod
    def calculate_rvol(
        df_today_15m_vol: float,
        df_hist_15m_avg_vol: float
    ) -> float:
        """
        Calculate Relative Volume (RVOL) comparing the current 15-min morning volume
        against historical 10-day average volume for the same 09:15-09:30 AM window.
        """
        if df_hist_15m_avg_vol <= 0:
            return 1.0
        return float(df_today_15m_vol / df_hist_15m_avg_vol)

    @staticmethod
    def calculate_gap_pct(open_today: float, prev_close: float) -> float:
        """Calculate gap percentage from previous day's closing price."""
        if prev_close <= 0:
            return 0.0
        return float(((open_today - prev_close) / prev_close) * 100.0)

    @staticmethod
    def calculate_orderbook_imbalance(depth_dict: Dict[str, Any]) -> float:
        """
        Calculate Order Book Imbalance ratio using Level 2 / Market Depth data.
        Returns ratio: total_bid_qty / (total_bid_qty + total_ask_qty) range [0.0, 1.0].
        Values > 0.6 indicate strong buy-side order book pressure.
        """
        try:
            buy_depth = depth_dict.get("buy", [])
            sell_depth = depth_dict.get("sell", [])

            total_bid_qty = sum(float(item.get("quantity", 0)) for item in buy_depth[:5])
            total_ask_qty = sum(float(item.get("quantity", 0)) for item in sell_depth[:5])

            total_qty = total_bid_qty + total_ask_qty
            if total_qty <= 0:
                return 0.5

            return float(total_bid_qty / total_qty)
        except Exception as exc:
            logger.error("Error computing orderbook imbalance: %s", exc)
            return 0.5

    @staticmethod
    def calculate_market_momentum(nifty_df: pd.DataFrame) -> float:
        """Calculate Nifty 50 15-minute log return return proxy."""
        if nifty_df.empty or len(nifty_df) < 2:
            return 0.0
        c_open = float(nifty_df.iloc[0]["open"])
        c_close = float(nifty_df.iloc[-1]["close"])
        if c_open <= 0:
            return 0.0
        return float(np.log(c_close / c_open) * 100.0)

    def extract_live_features(
        self,
        symbol: str,
        df_candles: pd.DataFrame,
        prev_close: float,
        hist_avg_vol_15m: float,
        depth_data: Dict[str, Any],
        nifty_df: pd.DataFrame
    ) -> pd.DataFrame:
        """
        Builds a single-row feature vector for a live OEL candidate ticker at 09:29:59 AM.
        """
        if df_candles.empty:
            raise ValueError(f"Empty candle DataFrame supplied for {symbol}")

        first_candle = df_candles.iloc[0]
        c_open = float(first_candle["open"])
        c_high = float(first_candle["high"])
        c_low = float(first_candle["low"])
        c_close = float(first_candle["close"])
        c_vol = float(df_candles["volume"].sum())

        candle_range = max(c_high - c_low, 0.05)
        upper_wick_pct = float(((c_high - max(c_open, c_close)) / candle_range) * 100.0)
        body_pct = float((abs(c_close - c_open) / candle_range) * 100.0)

        rvol = self.calculate_rvol(c_vol, hist_avg_vol_15m)
        gap_pct = self.calculate_gap_pct(c_open, prev_close)
        ob_imbalance = self.calculate_orderbook_imbalance(depth_data)
        nifty_mom = self.calculate_market_momentum(nifty_df)

        feature_dict = {
            "rvol": rvol,
            "gap_pct": gap_pct,
            "orderbook_imbalance": ob_imbalance,
            "nifty_momentum": nifty_mom,
            "upper_wick_pct": upper_wick_pct,
            "body_pct": body_pct,
            "price_level": c_close,
        }

        return pd.DataFrame([feature_dict])


# ===========================================================================
# 2. MACHINE LEARNING MODEL CLASS
# ===========================================================================

class OELSetupClassifier:
    """
    ML Classification Pipeline wrapper utilizing XGBoost / LightGBM with CalibratedClassifierCV.
    Strictly constrained hyperparameters to prevent overfitting on financial market noise.
    """

    def __init__(self, model_type: str = "xgboost"):
        self.model_type = model_type.lower()
        self.is_trained = False
        self.base_model = None
        self.calibrated_model = None

        if self.model_type == "xgboost" and XGBOOST_AVAILABLE:
            self.base_model = xgb.XGBClassifier(
                n_estimators=150,
                max_depth=3,
                learning_rate=0.03,
                subsample=0.8,
                colsample_bytree=0.8,
                min_child_weight=5,
                gamma=1.0,
                reg_alpha=0.1,
                reg_lambda=1.0,
                eval_metric="logloss",
                random_state=42
            )
        elif LIGHTGBM_AVAILABLE:
            self.base_model = lgb.LGBMClassifier(
                n_estimators=150,
                max_depth=3,
                num_leaves=7,
                learning_rate=0.03,
                subsample=0.8,
                colsample_bytree=0.8,
                min_child_samples=10,
                reg_alpha=0.1,
                reg_lambda=1.0,
                random_state=42
            )
        else:
            logger.warning("Neither XGBoost nor LightGBM is available. Using heuristic probability estimator.")

    def fit(self, X: pd.DataFrame, y: pd.Series, eval_set: Optional[List[Tuple[pd.DataFrame, pd.Series]]] = None) -> None:
        """Fit base model and calibrate probabilities via CalibratedClassifierCV (isotonic, cv=5)."""
        if self.base_model is None:
            logger.error("No ML framework available to train.")
            return

        logger.info("Training %s base classifier on %d records...", self.model_type, len(X))
        self.base_model.fit(X, y)

        cv_folds = min(5, max(2, len(X) // 10)) if len(X) >= 10 else 2
        try:
            self.calibrated_model = CalibratedClassifierCV(estimator=self.base_model, method="isotonic", cv=cv_folds)
            self.calibrated_model.fit(X, y)
            logger.info("CalibratedClassifierCV fitting complete with %d-fold CV.", cv_folds)
        except Exception as exc:
            logger.warning("CalibratedClassifierCV fit warning: %s. Using base model.", exc)
            self.calibrated_model = self.base_model

        self.is_trained = True
        logger.info("Model training and calibration complete.")

    def predict_probability(self, live_features: pd.DataFrame) -> float:
        """
        Returns calibrated confidence probability score [0.0 to 1.0] for target hit (Class 1).
        """
        if not self.is_trained:
            rvol = float(live_features.get("rvol", [1.0])[0])
            ob_imbalance = float(live_features.get("orderbook_imbalance", [0.5])[0])
            wick_pct = float(live_features.get("upper_wick_pct", [20.0])[0])
            
            score = 0.50
            if rvol > 1.5:
                score += 0.15
            if ob_imbalance > 0.60:
                score += 0.15
            if wick_pct < 15.0:
                score += 0.10
            return float(min(max(score, 0.0), 0.99))

        try:
            active_model = self.calibrated_model if self.calibrated_model is not None else self.base_model
            probs = active_model.predict_proba(live_features)
            return float(probs[0][1])  # Prob of Class 1
        except Exception as exc:
            logger.error("Inference failure: %s", exc)
            return 0.0

    def save_model(self, path: str) -> None:
        """Save calibrated model to disk using joblib."""
        try:
            target_obj = self.calibrated_model if self.calibrated_model is not None else self.base_model
            if target_obj:
                joblib.dump({"model": target_obj, "is_trained": self.is_trained, "model_type": self.model_type}, path)
                logger.info("Calibrated model saved to %s", path)
        except Exception as exc:
            logger.error("Failed to save model to %s: %s", path, exc)

    def load_model(self, path: str) -> None:
        """Load calibrated model from disk using joblib."""
        if os.path.exists(path):
            try:
                data = joblib.load(path)
                if isinstance(data, dict) and "model" in data:
                    self.calibrated_model = data["model"]
                    self.is_trained = data.get("is_trained", True)
                else:
                    self.calibrated_model = data
                    self.is_trained = True
                logger.info("Calibrated model loaded successfully from %s", path)
            except Exception as exc:
                logger.error("Failed to load model from %s: %s", path, exc)

    @staticmethod
    def generate_synthetic_training_data(n_samples: int = 5000) -> Tuple[pd.DataFrame, pd.Series]:
        """
        Generate synthetic training data based on the OEL strategy rules.
        Features reflect realistic distributions of morning breakout patterns.
        """
        np.random.seed(42)
        
        data = {
            "rvol": np.random.lognormal(mean=0.4, sigma=0.6, size=n_samples),
            "gap_pct": np.random.normal(loc=0.8, scale=0.6, size=n_samples),
            "orderbook_imbalance": np.clip(np.random.normal(loc=0.55, scale=0.15, size=n_samples), 0.1, 0.95),
            "nifty_momentum": np.random.normal(loc=0.15, scale=0.5, size=n_samples),
            "upper_wick_pct": np.random.exponential(scale=12.0, size=n_samples),
            "body_pct": np.random.uniform(30.0, 90.0, size=n_samples),
            "price_level": np.random.uniform(300.0, 3000.0, size=n_samples),
        }
        
        df = pd.DataFrame(data)
        
        prob = (
            0.30
            + 0.20 * (df["rvol"] > 1.8).astype(float)
            + 0.15 * (df["orderbook_imbalance"] > 0.60).astype(float)
            + 0.15 * (df["upper_wick_pct"] < 15).astype(float)
            + 0.10 * (df["gap_pct"] > 0.5).astype(float)
            + 0.10 * (df["nifty_momentum"] > 0).astype(float)
        )
        prob = np.clip(prob, 0.05, 0.95)
        y = (np.random.random(n_samples) < prob).astype(int)
        
        logger.info("Generated %d synthetic training samples (%d positive, %d negative)",
                     n_samples, int(y.sum()), n_samples - int(y.sum()))
        return df, pd.Series(y)

    def train_on_synthetic(self, n_samples: int = 5000) -> None:
        """Train the classifier on synthetic data generated from strategy rules."""
        X, y = self.generate_synthetic_training_data(n_samples)
        self.fit(X, y)
        logger.info("Model trained on %d synthetic samples. is_trained=%s", n_samples, self.is_trained)


class MarketAnomalyDetector:
    """
    IsolationForest-based Market Anomaly Detector (contamination=0.05).
    Fits on historical market metrics and exposes is_anomalous(today_metrics: dict) -> bool.
    """

    def __init__(self, contamination: float = 0.05):
        self.contamination = contamination
        self.model = IsolationForest(contamination=self.contamination, random_state=42)
        self.is_fitted = False

    def fit(self, historical_metrics: pd.DataFrame) -> None:
        """Fit IsolationForest on historical market metrics."""
        if historical_metrics.empty:
            logger.warning("Empty metrics DataFrame provided to MarketAnomalyDetector.")
            return

        logger.info("Fitting MarketAnomalyDetector on %d records...", len(historical_metrics))
        self.model.fit(historical_metrics)
        self.is_fitted = True
        logger.info("MarketAnomalyDetector fit complete.")

    def is_anomalous(self, today_metrics: dict) -> bool:
        """
        Check if today's market metrics represent an anomaly.
        Returns True if anomalous (-1 from IsolationForest), False if normal.
        """
        if not self.is_fitted:
            # Baseline synthetic fit if not fit yet
            np.random.seed(42)
            baseline = pd.DataFrame({
                "rvol": np.random.lognormal(0.4, 0.5, 500),
                "nifty_momentum": np.random.normal(0.1, 0.4, 500),
                "volatility": np.random.normal(1.2, 0.3, 500)
            })
            self.fit(baseline)

        try:
            # Map today's metrics to columns expected by model
            feature_keys = list(self.model.feature_names_in_) if hasattr(self.model, "feature_names_in_") else ["rvol", "nifty_momentum", "volatility"]
            metric_vec = {}
            for k in feature_keys:
                metric_vec[k] = float(today_metrics.get(k, 1.0 if k == "rvol" else 0.0))

            df_row = pd.DataFrame([metric_vec])
            pred = self.model.predict(df_row)
            is_anomaly = bool(pred[0] == -1)
            if is_anomaly:
                logger.warning("IsolationForest Market Anomaly Detected! Metrics: %s", today_metrics)
            return is_anomaly
        except Exception as exc:
            logger.error("MarketAnomalyDetector inference error: %s", exc)
            return False

    def save_model(self, path: str) -> None:
        """Save fitted IsolationForest model using joblib."""
        try:
            joblib.dump({"model": self.model, "is_fitted": self.is_fitted}, path)
            logger.info("MarketAnomalyDetector saved to %s", path)
        except Exception as exc:
            logger.error("Failed to save MarketAnomalyDetector to %s: %s", path, exc)

    def load_model(self, path: str) -> None:
        """Load fitted IsolationForest model using joblib."""
        if os.path.exists(path):
            try:
                data = joblib.load(path)
                if isinstance(data, dict):
                    self.model = data.get("model", self.model)
                    self.is_fitted = data.get("is_fitted", True)
                else:
                    self.model = data
                    self.is_fitted = True
                logger.info("MarketAnomalyDetector loaded successfully from %s", path)
            except Exception as exc:
                logger.error("Failed to load MarketAnomalyDetector from %s: %s", path, exc)


# ===========================================================================
# 3. DYNAMIC RISK MANAGEMENT MODULE
# ===========================================================================

class DynamicRiskManager:
    """
    Calculates dynamic ATR-based Stop Loss, Target projections, and Position Sizing.
    Replaces static 1:1.43 R:R ratios with asset-specific volatility sizing.
    """

    @staticmethod
    def calculate_atr(df_candles: pd.DataFrame, period: int = 14) -> float:
        """
        Compute Average True Range (ATR) vectorized over historical candles.
        """
        if df_candles.empty or len(df_candles) < period:
            # Fallback 1% price approximation
            return float(df_candles.iloc[-1]["close"] * 0.01) if not df_candles.empty else 5.0

        high = df_candles["high"].values
        low = df_candles["low"].values
        close = df_candles["close"].values

        prev_close = np.roll(close, 1)
        prev_close[0] = close[0]

        tr1 = high - low
        tr2 = np.abs(high - prev_close)
        tr3 = np.abs(low - prev_close)

        tr = np.maximum(tr1, np.maximum(tr2, tr3))
        atr = pd.Series(tr).rolling(window=period, min_periods=1).mean().iloc[-1]
        return float(atr)

    def calculate_trade_parameters(
        self,
        symbol: str,
        entry_price: float,
        candle_low: float,
        df_candles: pd.DataFrame,
        account_capital: float = 100000.0,
        risk_per_trade_pct: float = 0.01,
        confidence_score: float = 0.80
    ) -> TradeParams:
        """
        Dynamically calculates Stop Loss, Target 1 (1:2), Target 2 (1:3),
        and Risk-Adjusted Position Size based on ATR and Model Confidence.
        """
        atr = self.calculate_atr(df_candles, period=14)

        # Dynamic Stop-Loss Distance (Bounded by ATR buffer)
        structural_sl_dist = entry_price - candle_low
        atr_buffer = atr * 0.5
        sl_distance = max(structural_sl_dist + atr_buffer, atr * 0.8)
        
        stop_loss = round(entry_price - sl_distance, 2)

        # Scale Reward Multiplier based on Model Confidence Score (0.75 - 1.0)
        # Higher confidence = expanded profit target
        base_rr = 2.0 if confidence_score >= 0.85 else 1.75
        
        target_1 = round(entry_price + (sl_distance * base_rr), 2)
        target_2 = round(entry_price + (sl_distance * (base_rr + 1.0)), 2)

        # Dynamic Position Sizing (Fixed Fractional Risk Model)
        max_risk_amount = account_capital * risk_per_trade_pct
        position_size = max(int(max_risk_amount / sl_distance), 1)
        capital_allocated = round(position_size * entry_price, 2)

        return TradeParams(
            symbol=symbol,
            entry_price=round(entry_price, 2),
            stop_loss=stop_loss,
            target_1=target_1,
            target_2=target_2,
            risk_reward_ratio=round(base_rr, 2),
            position_size=position_size,
            capital_allocated=capital_allocated,
            atr=round(atr, 2),
            model_confidence=round(confidence_score, 4)
        )


# ===========================================================================
# 4. EXECUTION PIPELINE ORCHESTRATOR
# ===========================================================================

class OELTradingPipeline:
    """
    Main Orchestrator integrating Feature Engineering, ML Scoring Filter,
    and Dynamic ATR Risk Sizing into execution-ready orders.
    """

    def __init__(
        self,
        min_probability_threshold: float = 0.75,
        account_capital: float = 100000.0,
        risk_per_trade_pct: float = 0.01
    ):
        self.min_prob_threshold = min_probability_threshold
        self.account_capital = account_capital
        self.risk_per_trade_pct = risk_per_trade_pct

        self.feature_extractor = FeatureExtractor()
        self.classifier = OELSetupClassifier(model_type="xgboost")
        self.risk_manager = DynamicRiskManager()

        # Auto-train on synthetic data if no pre-trained model exists
        model_path = os.path.join(os.path.dirname(__file__), "oel_model.json")
        if os.path.exists(model_path):
            self.classifier.load_model(model_path)
            logger.info("Loaded pre-trained ML model from disk.")
        
        if not self.classifier.is_trained:
            logger.info("No pre-trained model found. Training on synthetic data...")
            self.classifier.train_on_synthetic(n_samples=5000)
            try:
                self.classifier.save_model(model_path)
            except Exception as exc:
                logger.debug("Could not persist model: %s", exc)

    def process_setup_candidates(
        self,
        candidate_tickers: List[Dict[str, Any]]
    ) -> List[TradeParams]:
        """
        Evaluates candidate OEL setup tickers at 09:29:59 AM IST.
        Only returns orders where ML probability exceeds threshold (>= 0.75).
        """
        approved_trades: List[TradeParams] = []

        for candidate in candidate_tickers:
            symbol = candidate["symbol"]
            df_candles = candidate["df_candles"]
            prev_close = candidate["prev_close"]
            hist_vol = candidate["hist_vol_15m"]
            depth_data = candidate.get("depth_data", {})
            nifty_df = candidate.get("nifty_df", pd.DataFrame())

            try:
                # 1. Compute Live Vectorized Feature Vector
                features_df = self.feature_extractor.extract_live_features(
                    symbol=symbol,
                    df_candles=df_candles,
                    prev_close=prev_close,
                    hist_avg_vol_15m=hist_vol,
                    depth_data=depth_data,
                    nifty_df=nifty_df
                )

                # 2. Predict Probability Score via ML Classifier
                prob_score = self.classifier.predict_probability(features_df)
                logger.info("Ticker %s ML Confidence Score: %.4f", symbol, prob_score)

                # 3. Execution Probability Filter Check (Prob >= 0.75)
                if prob_score >= self.min_prob_threshold:
                    entry_price = float(df_candles.iloc[-1]["close"])
                    candle_low = float(df_candles.iloc[0]["low"])

                    # 4. Compute Dynamic ATR Risk & Position Sizing
                    trade_params = self.risk_manager.calculate_trade_parameters(
                        symbol=symbol,
                        entry_price=entry_price,
                        candle_low=candle_low,
                        df_candles=df_candles,
                        account_capital=self.account_capital,
                        risk_per_trade_pct=self.risk_per_trade_pct,
                        confidence_score=prob_score
                    )

                    approved_trades.append(trade_params)
                    logger.info("✅ APPROVED TRADE: %s | Score: %.2f | Entry: %.2f | SL: %.2f | Size: %d",
                                symbol, prob_score, trade_params.entry_price, trade_params.stop_loss, trade_params.position_size)
                else:
                    logger.info("❌ REJECTED TRADE: %s | Score %.2f < Threshold %.2f",
                                symbol, prob_score, self.min_prob_threshold)

            except Exception as exc:
                logger.error("Error processing candidate %s: %s", symbol, exc)

        return approved_trades


# ===========================================================================
# VERIFICATION DEMO LAUNCHER
# ===========================================================================

if __name__ == "__main__":
    print("Initializing Quantitative ML Engine Architecture...")

    # Mock Candidate Data Generation
    mock_candles = pd.DataFrame([
        {"timestamp": "2026-07-28 09:15", "open": 1000.0, "high": 1025.0, "low": 1000.0, "close": 1020.0, "volume": 150000},
        {"timestamp": "2026-07-28 09:20", "open": 1020.0, "high": 1028.0, "low": 1018.0, "close": 1024.0, "volume": 80000},
        {"timestamp": "2026-07-28 09:25", "open": 1024.0, "high": 1032.0, "low": 1022.0, "close": 1030.0, "volume": 95000},
    ])

    mock_candidate = {
        "symbol": "RELIANCE",
        "df_candles": mock_candles,
        "prev_close": 990.0,
        "hist_vol_15m": 120000.0,
        "depth_data": {
            "buy": [{"quantity": 5000}, {"quantity": 3000}],
            "sell": [{"quantity": 2000}, {"quantity": 1000}]
        },
        "nifty_df": pd.DataFrame([
            {"open": 24000.0, "close": 24050.0},
            {"open": 24050.0, "close": 24110.0}
        ])
    }

    pipeline = OELTradingPipeline(min_probability_threshold=0.75, account_capital=200000.0, risk_per_trade_pct=0.01)
    results = pipeline.process_setup_candidates([mock_candidate])

    print("\n--- Pipeline Order Execution Output ---")
    for trade in results:
        print(trade)
