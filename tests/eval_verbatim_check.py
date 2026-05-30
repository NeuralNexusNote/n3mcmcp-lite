"""Verify verbatim recall by re-fetching the parent doc and byte-comparing."""
import json
import subprocess
import sys

ORIGINAL = """浮遊都市・不知火（しらぬい）設定資料 v0.3

【概要】
不知火は、雲海大陸シズマの上空3,200メートルに浮遊する人工都市である。建造は紀元1841年、技術者連合「青藍の塔」によって主導された。重力中和炉「シキミ-VII」を都市中央の昇降軸に据え、軸を起点として八方位に放射する八区画（瑞穂区・常磐区・神奈区・玻璃区・遠矢区・霜紅区・雲泉区・幽明区）が同心リング状に展開する。

【動力系】
シキミ-VIIは、深海熱水鉱床から採掘される結晶「鳴神鉱」を燃料とし、推力ではなく空間曲率の局所反転によって浮遊を維持する。この方式により都市全体の振動は地上建造物より小さく、住民は浮遊を体感しない。鳴神鉱の年間消費量は約47.2トンで、補給は月1回、雲海下層の専用昇降艇「夜雀」によって行われる。

【住民と統治】
登録住民は2026年現在で64,118名。都市議会は八区画から選出された各3名、計24名で構成され、議長は2年ごとに輪番で交代する。区画間の移動には鋼索電軌「環状青藍線」が使われ、片道12分で都市を一周する。

【気象と外殻】
都市外殻は六重結界「霞織り」によって守られており、雷・氷塊・気圧変動を遮断する。霞織りの維持は青藍の塔附属研究所「玻璃院」が担い、毎週月曜日の早朝に補強儀式が行われる慣習が続いている。"""


def jrpc(p, method, params=None, _id=1):
    msg = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        msg["params"] = params
    p.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
    p.stdin.flush()
    while True:
        raw = p.stdout.readline()
        if not raw:
            raise RuntimeError("server closed")
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
        if obj.get("id") == _id:
            return obj


def notify(p, method, params=None):
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    p.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
    p.stdin.flush()


def main():
    proc = subprocess.Popen(
        ["n3mc-workingmemory"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        jrpc(proc, "initialize", params={
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "verbatim-check", "version": "0.1"}}, _id=1)
        notify(proc, "notifications/initialized")
        resp = jrpc(proc, "tools/call",
                    params={"name": "search_memory",
                            "arguments": {
                                "query": "不知火 浮遊都市 鳴神鉱",
                                "session_id": "eval-§10-3-fictional",
                                "limit": 1}},
                    _id=2)
        text_blocks = resp["result"]["content"]
        body = next(b["text"] for b in text_blocks if b["type"] == "text")
        # Body has a header line "### [doc×2] [<id>] score=... <ts>" then blank, then content.
        lines = body.splitlines()
        # Skip header lines until the first non-header line that begins the doc
        # Original starts with "浮遊都市・不知火"
        idx = 0
        for i, ln in enumerate(lines):
            if ln.startswith("浮遊都市"):
                idx = i
                break
        recovered = "\n".join(lines[idx:]).rstrip()
        # Trim trailing reminder block if any (after a "\n---\n" plus "_Reminder")
        # In tool output the reminder is appended outside content[0].text in MCP shape
        # Just compare prefixes
        match_len = 0
        for a, b in zip(ORIGINAL, recovered):
            if a == b:
                match_len += 1
            else:
                break
        print(json.dumps({
            "original_chars": len(ORIGINAL),
            "recovered_chars": len(recovered),
            "matched_prefix_chars": match_len,
            "byte_identical_prefix": match_len == len(ORIGINAL),
            "first_diff_offset": None if match_len == len(ORIGINAL) else match_len,
            "first_diff_orig": None if match_len == len(ORIGINAL) else
                ORIGINAL[match_len:match_len+40],
            "first_diff_recv": None if match_len == len(ORIGINAL) else
                recovered[match_len:match_len+40],
            "tail_recovered": recovered[-80:],
            "tail_original": ORIGINAL[-80:],
        }, ensure_ascii=False, indent=2))
    finally:
        try: proc.stdin.close()
        except: pass
        try: proc.terminate(); proc.wait(timeout=5)
        except: proc.kill()


if __name__ == "__main__":
    main()
