import yaml
import pandas as pd
from pathlib import Path

_ROOT = Path(__file__).parent.parent
CONFIG_PATH = _ROOT / "config.yaml"


def load_config(path=None):
    p = Path(path) if path else CONFIG_PATH
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_maquina(path=None) -> str:
    """Retorna project.maquina do config.yaml (fallback: worksheet_name)."""
    cfg = load_config(path)
    proj = cfg.get("project", {})
    return proj.get("maquina") or proj.get("worksheet_name", "FB??")


def search_workbooks(name, content_filter="all"):
    from seeq import spy
    return spy.workbooks.search({"Name": name}, content_filter=content_filter)


def get_worksheet_items(workbook_id):
    from seeq import spy
    wbs = spy.workbooks.search({"ID": workbook_id}, content_filter="all")
    obj = spy.workbooks.pull(wbs)
    return obj[0].worksheets[0].display_items


def pull_data(config_path=None):
    from seeq import spy
    cfg = load_config(config_path)

    workbook_id = cfg["project"]["workbook_id"]
    time_delta_days = cfg["project"]["time_delta_days"]
    signals = cfg.get("signals", [])

    items_df = get_worksheet_items(workbook_id)

    user_tz = spy.utils.get_user_timezone(spy.session)
    end_time = pd.Timestamp.now(tz=user_tz)
    start_time = end_time - pd.Timedelta(days=time_delta_days)

    data = spy.pull(
        items_df[["ID", "Type"]],
        start=start_time.isoformat(),
        end=end_time.isoformat(),
        grid=None,
        header="ID",
    )

    if signals:
        rename_map = {s["id"]: s["name"] for s in signals if s.get("name")}
        data = data.rename(columns=rename_map)

    return data, items_df
