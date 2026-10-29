from __future__ import annotations

from pathlib import Path

import pytest

from config import Settings, _env_float


def test_hard_position_limit_cannot_be_bypassed() -> None:
    with pytest.raises(ValueError, match="MAX_POSITION_PCT"):
        Settings(max_position_pct=11.0)


def test_risk_per_trade_hard_limit() -> None:
    with pytest.raises(ValueError, match="RISK_PER_TRADE_PCT"):
        Settings(risk_per_trade_pct=3.0)


def test_drawdown_hard_limit() -> None:
    with pytest.raises(ValueError, match="MAX_DAILY_DRAWDOWN_PCT"):
        Settings(max_daily_drawdown_pct=5.1)


def test_tp_rr_requires_asymmetric_reward() -> None:
    with pytest.raises(ValueError, match="TP_RR"):
        Settings(tp_rr=1.2)


def test_live_mode_requires_keys(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="obrigatorias"):
        Settings(dry_run=False, data_dir=tmp_path)


def test_live_mode_requires_explicit_confirmation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="CONFIRM_LIVE_TRADING"):
        Settings(
            dry_run=False,
            api_key="key",
            api_secret="secret",
            data_dir=tmp_path,
        )


def test_live_mode_with_confirmation_is_valid(tmp_path: Path) -> None:
    settings = Settings(
        dry_run=False,
        api_key="key",
        api_secret="secret",
        confirm_live_trading="YES",
        data_dir=tmp_path,
        log_dir=tmp_path,
    )
    assert not settings.dry_run
    assert settings.state_path == tmp_path / "state.json"


def test_state_paths_are_separated_by_mode(tmp_path: Path) -> None:
    assert (
        Settings(data_dir=tmp_path).state_path
        == tmp_path / "state.dryrun.json"
    )
    assert (
        Settings(
            dry_run=False,
            sandbox=True,
            api_key="key",
            api_secret="secret",
            data_dir=tmp_path,
        ).state_path
        == tmp_path / "state.testnet.json"
    )


def test_state_file_cannot_escape_data_dir() -> None:
    with pytest.raises(ValueError, match="STATE_FILE"):
        Settings(state_file="../escape.json")


def test_float_env_rejects_non_finite_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_FLOAT", "inf")
    with pytest.raises(ValueError, match="finito"):
        _env_float("TEST_FLOAT", 1.0)


def test_from_env_reads_secrets_and_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("EXCHANGE_API_KEY", "abc")
    monkeypatch.setenv("EXCHANGE_API_SECRET", "def")
    settings = Settings.from_env()
    assert settings.dry_run
    assert settings.api_key == "abc"
    assert settings.api_secret == "def"
