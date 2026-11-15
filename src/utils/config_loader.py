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
    """保存时去掉内存里的密码和认证码，ctp_profiles 同样处理。"""
    cleaned: dict[str, Any] = {}
    for key, value in ctp.items():
        if key in _CTP_RUNTIME_KEYS:
            continue
        if key in {"sim", "live", "ctp_profiles"} and isinstance(value, dict):
            if key == "ctp_profiles":
                cleaned[key] = {
                    name: (
                        {inner: inner_val for inner, inner_val in item.items() if inner not in _CTP_RUNTIME_KEYS}
                        if isinstance(item, dict)
                        else item
                    )
                    for name, item in value.items()
                }
            else:
                cleaned[key] = {inner: inner_val for inner, inner_val in value.items() if inner not in _CTP_RUNTIME_KEYS}
        else:
            cleaned[key] = value
    return cleaned


def _read_ctp_secret(env_name: str, secrets: dict[str, Any], *fallbacks: str) -> str:
    """
    密码/认证码：先读环境变量，再读 secrets.local.yaml 同名小写键。

    找不到时返回空串。读到了就同步进环境变量，方便其它模块直接读。
    """
    name = str(env_name or "").strip()
    if name:
        value = os.environ.get(name, "")
        if value:
            return value
    for key in (name.lower(), *fallbacks):
        if not key:
            continue
        value = str(secrets.get(key, "") or "")
        if value:
            if name:
                os.environ[name] = value
            return value
    return ""


def _same_kind(value: Any, kind: str) -> bool:
    """配置里的 kind 可能是 live / 真实账号 / real。"""
    text = str(value or "").strip().lower()
    if kind == "live":
        return text in {"live", "real", "prod", "实盘", "真实", "真实账号"}
    return text not in {"live", "real", "prod", "实盘", "真实", "真实账号"}


def ensure_runtime_layout() -> Path:
    """
    打包后首次运行：把内置示例配置/模板拷到 exe 旁边（可改可写）。

    返回:
        项目根目录
    """
    root = project_root()
    res = resource_root()
    # 需要落在 exe 旁的目录（reviews 给收盘复盘存 txt/json，rules 给自定义规则策略）
    for name in ("config", "config/rules", "data", "data/reviews", "logs", "strategies"):
        (root / name).mkdir(parents=True, exist_ok=True)

    # 从打包资源复制缺省文件（不覆盖用户已有配置）
    pairs = [
        ("config/config.example.yaml", "config/config.example.yaml"),
        ("config/全品种备查.txt", "config/全品种备查.txt"),
        # 保证金/乘数只认这张总表，首次运行必须落到 exe 旁的 config 目录。
        ("config/品种与合约.xlsx", "config/品种与合约.xlsx"),
        # 优化参数表：网格档位和中文名都在这里，打包后落在 exe 旁方便直接改。
        ("config/优化参数.xlsx", "config/优化参数.xlsx"),
        # 自定义规则模板：rule_dsl 引擎的出厂规则，用户复制一份改成自己的。
        ("config/rules", "config/rules"),
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
            elif src_rel == "config/rules":
                # 升级时补上新增的出厂规则模板（新模板会直接出现在策略下拉里）；
                # 用户改过的同名文件一律不动。
                for item in src.glob("*.yaml"):
                    target = dst / item.name
                    if not target.exists():
                        shutil.copy2(item, target)
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


def _mapping_paths(data: Any, prefix: str = "") -> set[str]:
    """把嵌套字典展开成「a.b.c」键路径集合；只看键，不看值。"""
    if not isinstance(data, dict):
        return set()
    paths: set[str] = set()
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        paths.add(path)
        paths |= _mapping_paths(value, path)
    return paths


# 键由用户或品种表动态决定的小节，天然和示例配置不同，不参与键名比对。
_DYNAMIC_KEY_PREFIXES = ("data.symbol_map", "risk.contracts")


def _is_ignored_path(path: str) -> bool:
    """运行时字段（下划线开头）和动态小节跳过。"""
    if path.split(".", 1)[0].startswith("_"):
        return True
    return any(path == prefix or path.startswith(f"{prefix}.") for prefix in _DYNAMIC_KEY_PREFIXES)


def diff_config_keys(cfg: dict[str, Any], example: dict[str, Any]) -> tuple[list[str], list[str]]:
    """
    比对用户配置与示例配置的键名，返回 (未知键, 缺失键)。

    值是用户自己的，不比较；只找多写、少写、拼错的键。
    """
    got = {path for path in _mapping_paths(cfg) if not _is_ignored_path(path)}
    want = {path for path in _mapping_paths(example) if not _is_ignored_path(path)}
    return sorted(got - want), sorted(want - got)


def find_duplicate_keys(text: str) -> list[str]:
    """
    找出 YAML 同一层重复出现的键，返回「a.b」路径。

    yaml.safe_load 遇到重复键不报错，后写的直接覆盖前面的；这里只负责点名，不改变解析结果。
    文本解析失败时返回空列表，交给后续 safe_load 报真正的语法错误。
    """
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return []
    found: list[str] = []

    def walk(node: yaml.Node, prefix: str) -> None:
        if isinstance(node, yaml.MappingNode):
            seen: set[str] = set()
            for key_node, value_node in node.value:
                if not isinstance(key_node, yaml.ScalarNode):
                    continue
                key = str(key_node.value)
                path = f"{prefix}.{key}" if prefix else key
                if key in seen:
                    found.append(path)
                seen.add(key)
                walk(value_node, path)
        elif isinstance(node, yaml.SequenceNode):
            for item in node.value:
                walk(item, prefix)

    if root is not None:
        walk(root, "")
    return found


def _preview_keys(paths: list[str], limit: int = 8) -> str:
    """告警里只列前几个键，剩下的写个数，避免刷屏。"""
    shown = "、".join(paths[:limit])
    if len(paths) > limit:
        return f"{shown} 等 {len(paths)} 个"
    return shown


def _warn_config_key_diff(cfg: dict[str, Any], example: Path) -> None:
    """
    和 config.example.yaml 对键名，不中断加载。

    多写的键可能拼错或已废弃，记 warning；
    少写的键一律按程序默认值处理，属于正常裁剪，只在 debug 里提示。
    """
    try:
        with example.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except OSError as exc:
        logger.debug("读示例配置失败，跳过键名校验: {}", exc)
        return
    if not isinstance(data, dict):
        return
    unknown, missing = diff_config_keys(cfg, data)
    if unknown:
        logger.warning("配置里有示例没有的键（可能拼错或已废弃）: {}", _preview_keys(unknown))
    if missing:
        logger.debug("配置缺少示例里的键，按程序默认值处理: {}", _preview_keys(missing))


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
    example = root / "config" / "config.example.yaml"

    if not path.exists():
        logger.warning("未找到 {}，临时使用 {}", path, example)
        path = example

    text = path.read_text(encoding="utf-8")
    # 同一层写了两次的键，yaml 后写的会静默覆盖前面的；这里先点名，方便对照。
    for dup in find_duplicate_keys(text):
        logger.warning("配置里有重复键 {}（同一层写了两次，后写的生效）: {}", dup, path)
    cfg: dict[str, Any] = yaml.safe_load(text) or {}

    # 和示例配置对键名：多写/少写只告警，不阻断加载（旧配置仍可用）。
    if path != example:
        _warn_config_key_diff(cfg, example)

    # 最后一道保险：配置被裁剪或缺键时按模拟处理，绝不默认走实盘。
    cfg.setdefault("dry_run", True)

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
            from src.trader.ctp_profile import (
                DEFAULT_AUTH_CODE_ENV,
                DEFAULT_LIVE_PASSWORD_ENV,
                DEFAULT_SIM_PASSWORD_ENV,
            )

            auth_env = str(ctp.get("auth_code_env") or DEFAULT_AUTH_CODE_ENV)
            ctp["resolved_auth_code"] = _read_ctp_secret(auth_env, secrets)
            # 仿真密码、实盘密码、同花顺密码三套分开，互不顶替。
            ctp_password = _read_ctp_secret(DEFAULT_SIM_PASSWORD_ENV, secrets, "ctp_password")
            live_password = _read_ctp_secret(DEFAULT_LIVE_PASSWORD_ENV, secrets, "ctp_live_password")
            # 没单独写实盘密码时，CTP 真实通道用同一份交易密码（同花顺登录那一份）。
            if not live_password:
                live_password = password
            ctp["resolved_sim_password"] = ctp_password
            ctp["resolved_live_password"] = live_password
            ctp["resolved_password"] = ctp_password

            # 券商档案各自的密码/认证码环境变量。旧配置没有这一节时什么都不做。
            profiles = ctp.get("ctp_profiles")
            if isinstance(profiles, dict):
                for item in profiles.values():
                    if not isinstance(item, dict):
                        continue
                    kind_value = item.get("kind")
                    if kind_value is None:
                        kind_value = ctp.get("account_kind")
                    live_profile = _same_kind(kind_value, "live")
                    pwd_env = str(
                        item.get("password_env")
                        or (DEFAULT_LIVE_PASSWORD_ENV if live_profile else DEFAULT_SIM_PASSWORD_ENV)
                    )
                    pwd = _read_ctp_secret(pwd_env, secrets)
                    if not pwd and live_profile:
                        pwd = live_password or password
                    item["resolved_password"] = pwd
                    code_env = item.get("auth_code_env")
                    if code_env is None:
                        code_env = auth_env
                    code_env = str(code_env or "")
                    item["resolved_auth_code"] = (
                        _read_ctp_secret(code_env, secrets) if code_env else ""
                    )

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
    # 缺 config.yaml 时 load_config 会临时读 config.example.yaml；保存不能把示例模板改掉，
    # 这种情况一律写回正式的 config.yaml。
    if path.name == "config.example.yaml":
        path = root / "config" / "config.yaml"

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
    # 先写临时文件再原子替换：写到一半断电/崩溃时，原配置仍是完整的一份。
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    logger.info("配置已保存: {}", path)
    return path
