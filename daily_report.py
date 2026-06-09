import json
import logging
import os
import smtplib
import ssl
import sys
import threading
import time
from datetime import date, timedelta
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
import schedule
from PIL import Image, ImageDraw
import pystray

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
CFG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "daily_report.log"

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8")],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CFG_PATH, encoding="utf-8") as f:
        return json.load(f)

# ── AINOS API ─────────────────────────────────────────────────────────────────

def api_get(cfg: dict, path: str, params: dict = None) -> dict:
    url = f"http://{cfg['ainos_host']}:{cfg['ainos_port']}{path}"
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def api_get_bytes(cfg: dict, path: str) -> bytes:
    url = f"http://{cfg['ainos_host']}:{cfg['ainos_port']}{path}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content

# ── HTML 빌더 ─────────────────────────────────────────────────────────────────

H_NAVY   = "#1a3a6b"
H_BLUE   = "#2b5797"
H_BORDER = "#c5d5ea"
H_EVEN   = "#f0f4fa"
H_TOTAL  = "#dce8f8"
H_WHITE  = "#ffffff"

def _td(val, align="center", extra=""):
    return f'<td style="padding:8px 12px;border:1px solid {H_BORDER};text-align:{align};{extra}">{val}</td>'

def _th(val, align="center", extra=""):
    return (f'<th style="padding:10px 12px;border:1px solid {H_BORDER};text-align:{align};'
            f'color:{H_WHITE};background:{H_BLUE};{extra}">{val}</th>')

def build_precision_table(events: list) -> str:
    rows = ""
    for i, ev in enumerate(events):
        bg   = H_EVEN if i % 2 == 0 else H_WHITE
        rows += (
            f'<tr style="background:{bg}">'
            + _td(ev["event"], "left", "font-weight:600;")
            + _td(f'{ev["jeongdam"]:,}', "center", "color:#2563a8;font-weight:600;")
            + _td(f'{ev["odam"]:,}',     "center", "color:#c0392b;font-weight:600;")
            + _td(f'{ev["total"]:,}',    "center", "font-weight:600;")
            + "</tr>"
        )

    tj = sum(e["jeongdam"] for e in events)
    to = sum(e["odam"]     for e in events)
    tt = sum(e["total"]    for e in events)
    rows += (
        f'<tr style="background:{H_TOTAL};font-weight:700;">'
        + _td("합계", "left")
        + _td(f"{tj:,}", "center", "color:#2563a8;")
        + _td(f"{to:,}", "center", "color:#c0392b;")
        + _td(f"{tt:,}", "center")
        + "</tr>"
    )

    header = (
        "<tr>"
        + _th("이벤트", "left")
        + _th("정탐")
        + _th("오탐")
        + _th("합계")
        + "</tr>"
    )
    return (
        f'<table style="border-collapse:collapse;width:100%;font-size:14px;'
        f'font-family:Malgun Gothic,sans-serif;">'
        f"<thead>{header}</thead><tbody>{rows}</tbody></table>"
    )

def build_server_table(viewers: list, all_events: list) -> str:
    active_ev = [e for e in all_events if any(v["events"].get(e, 0) > 0 for v in viewers)]

    header = (
        "<tr>"
        + _th("서버 이름", "left")
        + "".join(_th(e) for e in active_ev)
        + _th("합계")
        + "</tr>"
    )

    rows = ""
    for i, v in enumerate(viewers):
        bg    = H_EVEN if i % 2 == 0 else H_WHITE
        cells = "".join(_td(f'{v["events"].get(e, 0):,}') for e in active_ev)
        rows += (
            f'<tr style="background:{bg}">'
            + _td(v["viewer_name"], "left", "font-weight:600;white-space:nowrap;")
            + cells
            + _td(f'{v["total"]:,}', "center", f"font-weight:700;color:{H_BLUE};")
            + "</tr>"
        )

    totals    = {e: sum(v["events"].get(e, 0) for v in viewers) for e in active_ev}
    grand_tot = sum(v["total"] for v in viewers)
    total_cells = "".join(_td(f"{totals[e]:,}") for e in active_ev)
    rows += (
        f'<tr style="background:{H_TOTAL};font-weight:700;">'
        + _td("합계", "left")
        + total_cells
        + _td(f"{grand_tot:,}", "center", f"color:{H_BLUE};")
        + "</tr>"
    )

    return (
        f'<table style="border-collapse:collapse;width:100%;font-size:13px;'
        f'font-family:Malgun Gothic,sans-serif;">'
        f"<thead>{header}</thead><tbody>{rows}</tbody></table>"
    )

KR_DAYS = ["월", "화", "수", "목", "금", "토", "일"]

def build_html(target: date, precision: dict, server: dict) -> str:
    wd       = KR_DAYS[target.weekday()]
    date_str = f"{target.year}년 {target.month:02d}월 {target.day:02d}일 ({wd}요일)"
    summary  = precision.get("summary", {})

    prec_table   = build_precision_table(precision.get("events", []))
    server_table = build_server_table(server.get("viewers", []), server.get("events", []))

    def section(num, title, subtitle, content):
        return f"""
        <div style="background:{H_WHITE};padding:24px 32px;margin-top:3px;
                    border-left:1px solid {H_BORDER};border-right:1px solid {H_BORDER};">
          <div style="font-size:15px;font-weight:700;color:{H_NAVY};margin-bottom:14px;
                      padding-bottom:10px;border-bottom:2px solid {H_BLUE};">
            {num} {title}
            <span style="font-size:12px;font-weight:400;color:#777;margin-left:8px;">{subtitle}</span>
          </div>
          {content}
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#eef2f8;font-family:Malgun Gothic,sans-serif;">
<div style="max-width:920px;margin:0 auto;padding:28px 16px;">

  <div style="background:{H_NAVY};border-radius:10px 10px 0 0;padding:28px 32px;color:{H_WHITE};">
    <div style="font-size:12px;letter-spacing:3px;opacity:0.7;margin-bottom:8px;">DANUSYS · AINOS PLATFORM</div>
    <div style="font-size:26px;font-weight:700;margin-bottom:4px;">일일 보고서</div>
    <div style="font-size:16px;opacity:0.85;">{date_str}</div>
  </div>

  <table style="width:100%;background:{H_BLUE};border-collapse:collapse;">
    <tr>
      <td style="padding:13px 32px;color:{H_WHITE};font-size:14px;">
        전체&nbsp;<strong>{summary.get("total", 0):,}건</strong>
        &emsp;|&emsp;
        정탐&nbsp;<strong style="color:#7ec8f7;">{summary.get("jeongdam", 0):,}건</strong>
        &emsp;|&emsp;
        오탐&nbsp;<strong style="color:#ffaaaa;">{summary.get("odam", 0):,}건</strong>
        &emsp;|&emsp;
        정탐율&nbsp;<strong style="color:#7ec8f7;">{summary.get("precision", 0)}%</strong>
      </td>
    </tr>
  </table>

  {section("①", "이벤트별 정탐 / 오탐 현황", str(target), prec_table)}
  {section("②", "서버별 이벤트 현황", "최근 14일 누적", server_table)}
  {section("③", "최근 14일 이벤트 통계", "",
           '<img src="cid:chart_histogram" style="width:100%;border-radius:6px;" alt="14일 통계">')}

  <div style="background:{H_NAVY};border-radius:0 0 10px 10px;padding:14px 32px;
              color:rgba(255,255,255,0.5);font-size:12px;text-align:center;">
    이 메일은 DANUSYS AINOS 플랫폼에서 자동 발송되었습니다.
  </div>

</div>
</body>
</html>"""

# ── 메일 발송 ─────────────────────────────────────────────────────────────────

def send_email(cfg: dict, subject: str, html: str, chart_bytes):
    msg            = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"]    = cfg["smtp_user"]
    msg["To"]      = ", ".join(cfg["recipients"])

    alt = MIMEMultipart("alternative")
    msg.attach(alt)
    alt.attach(MIMEText(html, "html", "utf-8"))

    if chart_bytes:
        img = MIMEImage(chart_bytes)
        img.add_header("Content-ID", "<chart_histogram>")
        img.add_header("Content-Disposition", "inline", filename="histogram.png")
        msg.attach(img)

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], context=ctx) as smtp:
        smtp.login(cfg["smtp_user"], cfg["smtp_pass"])
        smtp.sendmail(cfg["smtp_user"], cfg["recipients"], msg.as_bytes())

# ── 보고서 실행 ───────────────────────────────────────────────────────────────

def run_report(target: date = None):
    log.info("=== 일일 보고서 시작 ===")
    cfg    = load_config()
    target = target or (date.today() - timedelta(days=1))
    wd     = KR_DAYS[target.weekday()]
    subject = f"[AINOS] {target.year}년 {target.month:02d}월 {target.day:02d}일 ({wd}요일) 일일 보고서"

    try:
        precision = api_get(cfg, "/api/analysis/precision", {"target_date": str(target)})
        log.info("정탐/오탐 데이터 수신 완료")
    except Exception as e:
        log.error("정탐/오탐 수신 실패: %s", e)
        return

    try:
        server = api_get(cfg, "/api/server/stats", {"ref_date": str(target)})
        log.info("서버 통계 수신 완료")
    except Exception as e:
        log.error("서버 통계 수신 실패: %s", e)
        server = {"viewers": [], "events": []}

    chart_bytes = None
    try:
        chart_bytes = api_get_bytes(cfg, f"/api/stats/histogram/image?ref_date={target}")
        log.info("차트 이미지 수신 완료 (%d bytes)", len(chart_bytes))
    except Exception as e:
        log.error("차트 이미지 수신 실패: %s", e)

    html = build_html(target, precision, server)

    try:
        send_email(cfg, subject, html, chart_bytes)
        log.info("메일 발송 완료 → %s", cfg["recipients"])
    except Exception as e:
        log.error("메일 발송 실패: %s", e)
        return

    log.info("=== 일일 보고서 완료 ===")

# ── Tray Icon ─────────────────────────────────────────────────────────────────

def make_icon_image():
    img  = Image.new("RGB", (64, 64), H_NAVY)
    draw = ImageDraw.Draw(img)
    draw.rectangle([2, 2, 61, 61], outline="#5599dd", width=2)
    draw.text((10, 16), "AI", fill=H_WHITE)
    draw.text((8, 36), "NOS", fill="#7ec8f7")
    return img

def on_send_now(icon, item):
    threading.Thread(target=run_report, daemon=True).start()

def on_open_log(icon, item):
    os.startfile(str(LOG_PATH))

def on_quit(icon, item):
    icon.stop()

# ── 스케줄러 스레드 ───────────────────────────────────────────────────────────

def scheduler_loop(send_time: str):
    log.info("스케줄러 시작 — 발송 시간: 매일 %s", send_time)
    schedule.every().day.at(send_time).do(run_report)
    while True:
        schedule.run_pending()
        time.sleep(30)

# ── 메인 ─────────────────────────────────────────────────────────────────────

def main():
    cfg       = load_config()
    send_time = f"{cfg.get('send_hour', 6):02d}:{cfg.get('send_minute', 0):02d}"

    t = threading.Thread(target=scheduler_loop, args=(send_time,), daemon=True)
    t.start()

    menu = pystray.Menu(
        pystray.MenuItem("DANUSYS AINOS 보고서", None, enabled=False),
        pystray.MenuItem(f"발송 시간: 매일 {send_time}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("지금 발송", on_send_now),
        pystray.MenuItem("로그 보기", on_open_log),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("종료", on_quit),
    )
    icon = pystray.Icon("AINOS", make_icon_image(), f"AINOS 보고서 ({send_time})", menu)
    icon.run()

if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--test":
        try:
            test_date = date.fromisoformat(sys.argv[2])
        except ValueError:
            print("날짜 형식 오류 — 예: --test 2026-03-10")
            sys.exit(1)

        # 테스트 모드: 콘솔 로그도 출력
        logging.getLogger().addHandler(logging.StreamHandler())
        print(f"테스트 모드: {test_date} 날짜로 즉시 발송")
        run_report(target=test_date)
    else:
        main()
