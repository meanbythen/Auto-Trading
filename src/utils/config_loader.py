"""
配置加载模块
功能：读取 YAML 配置；密码优先环境变量，其次本地 secrets.local.yaml（不进 git）。
原理：业务参数写在 config.yaml；敏感信息与主配置分离，图形界面可改账号密码。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from src.utils.paths import project_root, resource_root


def _project_root() -> Path:
    """返回可读写项目根目录（源码仓 / exe 旁）。"""
    return project_root()


def _drop_dead_keys(cfg: dict[str, Any]) -> None:
    """
    丢掉代码从不读取的旧键，加载和保存都清一次。

    strategy.enabled：策略启停早已由 engine 决定；
    account.note：早期给人写备注用的字段，程序不读。
    """
    strategy = cfg.get("strategy")
    if isinstance(strategy, dict):
        strategy.pop("enabled", None)
    account = cfg.get("account")
    if isinstance(account, dict):
        account.pop("note", None)


_CTP_RUNTIME_KEYS = {
    "resolved_auth_code",
    "resolved_password",
    "resolved_sim_password",
    "resolved_live_password",
}


def _strip_ctp_runtime(ctp: dict[str, Any]) -> dict[str, Any]:
    """保存时去掉内存里的密码和认证码，sim/live 小节同样处理。"""
    cleaned: dict[str, Any] = {}
    for key, value in ctp.items():
        if key in _CTP_RUNTIME_KEYS:
            continue
        if key in {"sim", "live"} and isinstance(value, dict):
            cleaned[key] = {inner: inner_val for inner, inner_val in value.items() if inner not in _CTP_RUNTIME_KEYS}
        else:
            cleaned[key] = value
    return cleaned


def ensure_runtime_layout() -> Path:
    """
    打包后首次运行：把内置示例配置/模板拷到 exe 旁边（可改可写）。

    返回:
        项目根目录
    """
    root = project_root()
    res = resource_root()
    # 需要落在 exe 旁的目录（reviews 给收盘复盘存 txt/json）
    for name in ("config", "data", "data/reviews", "logs", "strategies"):
        (root / name).mkdir(parents=True, exist_ok=True)

    # 从打包资源复制缺省文件（不覆盖用户已有配置）
    pairs = [
        ("config/config.example.yaml", "config/config.example.yaml"),
        ("config/全品种备查.txt", "config/全品种备查.txt"),
        # 保证金/乘数只认这张总表，首次运行必须落到 exe 旁的 config 目录。
        ("config/品种与合约.xlsx", "config/品种与合约.xlsx"),
        # 优化参数表：网格档位和中文名都在这里，打包后落在 exe 旁方便直接改。
        ("config/优化参数.xlsx", "config/优化参数.xlsx"),
        ("strategies", "strategies"),
        ("README.md", "README.md"),
        ("CHANGELOG.md", "CHANGELOG.md"),
        ("VERSION", "VERSION"),
    ]
    # 主力合约表：回测按它的「主力时段」自动换月。年份换了也不必改代码，按文件名拷过去。
    for table in sorted((res / "config").glob("*主力合约表*.xlsx")):
        pairs.append((f"config/{table.name}", f"config/{table.name}"))
    for src_rel, dst_rel in pairs:
        src = res / src_rel
        dst = root / dst_rel
        if not src.exists():
            continue
        if src.is_dir():
            if not dst.exists() or not any(dst.iterdir()):
                shutil.copytree(src, dst, dirs_exist_ok=True)
        elif not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    cfg_path = root / "config" / "config.yaml"
    example = root / "config" / "config.example.yaml"
    # 也尝试从资源直接拿 example
    if not example.exists():
        bundled = res / "config" / "config.example.yaml"
        if bundled.exists():
            shutil.copy2(bundled, example)
    if not cfg_path.exists() and example.exists():
        shutil.copy2(example, cfg_path)
        logger.info("首次运行：已生成 config/config.yaml（来自示例，请按需修改）")
    return root


def secrets_path() -> Path:
    """本地密钥文件路径（gitignore）。"""
    return _project_root() / "config" / "secrets.local.yaml"


def load_secrets() -> dict[str, Any]:
    """读取本地密钥；不存在则返回空字典。"""
    path = secrets_path()
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def save_secrets(password: str = "", extra: dict[str, Any] | None = None) -> Path:
    """
    保存本地密钥。

    参数:
        password: 交易密码（可空表示不改已有密码）
        extra: 其它可存本地的敏感项
    """
    path = secrets_path()
    data = load_secrets()
    if password != "":
        data["password"] = password
    if extra:
        data.update(extra)
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return path
    path.write_text(text, encoding="utf-8")
    logger.info("本地密钥已保存: {}", path)
    return path


def load_config(
    config_path: str | Path | None = None,
    *,
    apply_universe: bool = True,
) -> dict[str, Any]:
    """
    加载配置文件。

    apply_universe=False 时不读品种表。保存前只合并同花顺坐标时用，避免又打开一遍 Excel。
    返回:
        解析后的字典；含 resolved_password（环境变量 > secrets.local.yaml）
    """
    root = ensure_runtime_layout()
    path = Path(config_path) if config_path else root / "config" / "config.yaml"

    if not path.exists():
        example = root / "config" / "config.example.yaml"
        logger.warning("未找到 {}，临时使用 {}", path, example)
        path = example

    with path.open("r", encoding="utf-8") as f:
        cfg: dict[str, Any] = yaml.safe_load(f) or {}

    secrets = load_secrets()
    pwd_env = cfg.get("account", {}).get("password_env", "FUTURES_PASSWORD")
    password = os.environ.get(pwd_env, "")
    if not password:
        password = str(secrets.get("password", "") or "")
        if password:
            # 同步到当前进程环境，方便其它模块读取
            os.environ[pwd_env] = password

    cfg.setdefault("account", {})["resolved_password"] = password

    # 认证码和密码一样，只放环境变量或本地密钥，不写进 config.yaml，也不打日志。
    trade = cfg.setdefault("trade", {})
    if isinstance(trade, dict):
        ctp = trade.setdefault("ctp", {})
        if isinstance(ctp, dict):
            auth_env = str(ctp.get("auth_code_env") or "CTP_AUTH_CODE")
            auth_code = os.environ.get(auth_env, "")
            if not auth_code:
                auth_code = str(secrets.get("ctp_auth_code", "") or "")
                if auth_code:
                    os.environ[auth_env] = auth_code
            ctp["resolved_auth_code"] = auth_code
            # 仿真密码、实盘密码、同花顺密码三套分开，互不顶替。
            ctp_password = os.environ.get("CTP_PASSWORD", "")
            if not ctp_password:
                ctp_password = str(secrets.get("ctp_password", "") or "")
            live_password = os.environ.get("CTP_LIVE_PASSWORD", "")
            if not live_password:
                live_password = str(secrets.get("ctp_live_password", "") or "")
            # 没单独写实盘密码时，CTP 真实通道用同一份交易密码（同花顺登录那一份）。
            if not live_password:
                live_password = password
            ctp["resolved_sim_password"] = ctp_password
            ctp["resolved_live_password"] = live_password
            ctp["resolved_password"] = ctp_password

    if not password:
        logger.warning(
            "未检测到密码（环境变量 {} 或 config/secrets.local.yaml），"
            "自动登录无法填密码（模拟模式仍可运行）",
            pwd_env,
        )

    # 丢掉已删策略残留，并补上现有策略缺的默认小节（不改用户已写的数字）。
    try:
        from src.strategy.factory import sanitize_strategy_config

        sanitize_strategy_config(cfg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理策略配置时出错，仍继续加载: {}", exc)

    # 已删除「最大持仓手数」：旧配置里若还留着这一项，加载时丢掉。
    risk = cfg.get("risk")
    if isinstance(risk, dict):
        risk.pop("max_position", None)
    _drop_dead_keys(cfg)

    cfg["_project_root"] = str(root)
    cfg["_config_path"] = str(path)

    if apply_universe:
        # 品种与合约总表：关注合约 + 保证金/乘数只认这一张 Excel。
        try:
            from src.risk.universe_table import apply_universe_table_to_cfg

            apply_universe_table_to_cfg(cfg, create_if_missing=True)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "读取品种与合约表失败，暂时沿用配置文件里的名单和保证金。"
                "配置文件里的乘数/保证金可能是旧值，请检查 config/品种与合约.xlsx: {}",
                exc,
            )

    return cfg


def save_config(cfg: dict[str, Any], config_path: str | Path | None = None) -> Path:
    """
    把配置写回 YAML（供图形界面保存）。

    注意：不写入 resolved_password / _project_root 等运行时字段。
    保存前再清一次已删策略残留，避免旧小节又写回磁盘。
    保存前再读一次品种与合约总表，避免界面旧缓存盖掉你在 Excel 里刚改的数。
    """
    root = _project_root()
    path = Path(config_path) if config_path else Path(cfg.get("_config_path") or root / "config" / "config.yaml")

    try:
        from src.strategy.factory import sanitize_strategy_config

        sanitize_strategy_config(cfg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("保存前清理策略配置时出错: {}", exc)

    try:
        from src.risk.universe_table import apply_universe_table_to_cfg

        apply_universe_table_to_cfg(cfg, create_if_missing=True)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "保存前读取品种与合约表失败，仍按当前内存配置写入。"
            "请检查 config/品种与合约.xlsx: {}",
            exc,
        )

    dump = {k: v for k, v in cfg.items() if not str(k).startswith("_")}
    risk_dump = dump.get("risk")
    if isinstance(risk_dump, dict):
        risk_dump.pop("max_position", None)
    _drop_dead_keys(dump)
    if "account" in dump and isinstance(dump["account"], dict):
        dump["account"] = {k: v for k, v in dump["account"].items() if k != "resolved_password"}
    trade_dump = dump.get("trade")
    if isinstance(trade_dump, dict) and isinstance(trade_dump.get("ctp"), dict):
        trade_dump["ctp"] = _strip_ctp_runtime(trade_dump["ctp"])

    text = yaml.safe_dump(dump, allow_unicode=True, sort_keys=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return path
    path.write_text(text, encoding="utf-8")
    logger.info("配置已保存: {}", path)
    return path
