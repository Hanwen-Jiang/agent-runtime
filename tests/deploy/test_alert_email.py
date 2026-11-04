"""F4 的告警通知渠道（2026-11-04 用户定）：Grafana 经 Resend 的 SMTP 中继发邮件。

- SMTP 的用户名与口令（Resend 的 API Key）只从 Secret grafana-resend 来，values 里只写名字与键；
- 发件经 smtp.resend.com 的 587 端口，强制 STARTTLS；
- 收件地址也在 Secret 里（不进仓库），经环境变量进联络点；
- 所有告警走这一个联络点；
- install.sh 开装前核对这个 Secret 在；示例里有它（占位值）。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "deploy" / "data"
VALUES = yaml.safe_load((DATA / "values" / "grafana.yaml").read_text("utf-8"))


def test_smtp_credentials_come_from_a_secret() -> None:
    assert VALUES["smtp"] == {"existingSecret": "grafana-resend", "userKey": "user", "passwordKey": "password"}
    smtp = VALUES["grafana.ini"]["smtp"]
    assert smtp["enabled"] is True
    assert smtp["host"] == "smtp.resend.com:587"
    assert smtp["startTLS_policy"] == "MandatoryStartTLS"
    assert "password" not in smtp and "user" not in smtp, "凭据不写进 grafana.ini"
    assert smtp["from_address"].endswith("@cgent.link")


def test_contact_point_reads_the_recipient_from_the_secret() -> None:
    ref = VALUES["envValueFrom"]["ALERT_EMAIL_TO"]["secretKeyRef"]
    assert ref == {"name": "grafana-resend", "key": "to"}
    [cp] = VALUES["alerting"]["contactpoints.yaml"]["contactPoints"]
    [receiver] = cp["receivers"]
    assert receiver["type"] == "email"
    assert receiver["settings"]["addresses"] == "$ALERT_EMAIL_TO"
    text = (DATA / "values" / "grafana.yaml").read_text("utf-8")
    assert not re.search(r"[\w.+-]+@(?!cgent\.link)[\w-]+\.[\w.]+", text), "仓库里不写收件地址"


def test_every_alert_goes_to_the_email_contact_point() -> None:
    [cp] = VALUES["alerting"]["contactpoints.yaml"]["contactPoints"]
    [policy] = VALUES["alerting"]["policies.yaml"]["policies"]
    assert policy["receiver"] == cp["name"]


def test_install_checks_the_secret_and_the_example_has_it() -> None:
    install = (DATA / "install.sh").read_text("utf-8")
    assert "grafana-resend" in install
    example = (ROOT / "deploy" / "cluster" / "secrets" / "secrets.example.yaml").read_text("utf-8")
    docs = [d for d in yaml.safe_load_all(example) if d and d.get("kind") == "Secret"]
    [secret] = [d for d in docs if d["metadata"]["name"] == "grafana-resend"]
    assert secret["metadata"]["namespace"] == "observability"
    assert secret["stringData"]["user"] == "resend"
    assert set(secret["stringData"]) == {"user", "password", "to"}
