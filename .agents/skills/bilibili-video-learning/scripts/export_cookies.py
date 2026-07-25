#!/usr/bin/env python3
"""
Export Bilibili cookies from browser for use with yt-dlp / API downloads.
Usage: python export_cookies.py [browser] [--output cookies.txt]

Supported browsers: edge (default), chrome, firefox
"""
import sys
import os
import sqlite3
import shutil
import tempfile
from pathlib import Path

from runtime_output import log  # Cookie 诊断写 stderr，避免和未来 JSON 输出混在一起。


# --- 只把浏览器已经解密的 Cookie 转成 Netscape 行 ---
def build_netscape_cookie_lines(rows):
    lines = ["# Netscape HTTP Cookie File", "# Generated for one local session", ""]
    exported_count = 0                                          # 成功口径只统计真正写入值的 Cookie。
    for host, name, value, _encrypted_value in rows:
        if not value:                                           # Chrome/Edge 的 DPAPI 密文不能当成可用值。
            continue
        domain_flag = "TRUE" if host.startswith(".") else "FALSE"
        lines.append(f"{host}\t{domain_flag}\t/\tFALSE\t9999999999\t{name}\t{value}")
        exported_count += 1
    return lines, exported_count


# --- 安全写入一份确实含有明文值的 Cookie 文件 ---
def write_netscape_cookie_file(rows, output_path):
    lines, exported_count = build_netscape_cookie_lines(rows)
    if exported_count == 0:                                     # 保留既有文件，避免空结果覆盖可用凭据。
        log("No decryptable Bilibili cookies were exported; browser values are DPAPI encrypted.")
        return False
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)             # 显式输出目录由用户决定。
    output.write_text("\n".join(lines), encoding="utf-8")
    try:
        os.chmod(output, 0o600)                                  # POSIX 收紧权限；Windows 仍需依赖当前用户 ACL。
    except OSError:
        pass                                                     # 权限收紧失败不改变已经写入的数据语义。
    log(f"Exported {exported_count} decryptable cookies to: {output}")
    return True


def export_edge_cookies(output_path):
    """Try to export cookies from Edge's Network/Cookies database."""
    # Edge stores cookies in two locations
    edge_profile = os.path.join(
        os.environ["LOCALAPPDATA"],
        "Microsoft", "Edge", "User Data", "Default"
    )

    # Try Network/Cookies first (version 130+)
    cookie_db = os.path.join(edge_profile, "Network", "Cookies")
    if not os.path.exists(cookie_db):
        cookie_db = os.path.join(edge_profile, "Cookies")

    if not os.path.exists(cookie_db):
        print(f"ERROR: Cookie database not found at {cookie_db}")
        print("Edge may not be installed or using a different profile.")
        return False

    # Copy to temp (bypasses file lock)
    temporary_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp = temporary_file.name                                   # 先创建唯一文件，避免 mktemp 路径竞争。
    temporary_file.close()
    try:
        shutil.copy2(cookie_db, tmp)
    except PermissionError:
        os.unlink(tmp)                                           # 复制失败时移除刚创建的空临时文件。
        print("ERROR: Edge is running and locks its cookie database.")
        print("")
        print("Options:")
        print("  1. Close Edge, then run: python export_cookies.py edge")
        print("  2. Install 'EditThisCookie' extension in Edge")
        print("     → Go to bilibili.com")
        print("     → Click extension icon → Export (Netscape format)")
        print("     → Save as bilibili_cookies.txt")
        return False

    # Extract bilibili cookies
    try:
        conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        cursor = conn.execute(
            "SELECT host_key, name, value, encrypted_value FROM cookies "
            "WHERE host_key LIKE '%bilibili%'"
        )
        rows = cursor.fetchall()
        conn.close()
    except Exception as e:
        print(f"ERROR reading cookies: {e}")
        os.unlink(tmp)
        return False

    os.unlink(tmp)

    if not rows:
        print("No Bilibili cookies found. Are you logged into bilibili.com?")
        return False

    return write_netscape_cookie_file(rows, output_path)


def export_chrome_cookies(output_path):
    """Similar for Chrome."""
    chrome_profile = os.path.join(
        os.environ["LOCALAPPDATA"],
        "Google", "Chrome", "User Data", "Default"
    )
    cookie_db = os.path.join(chrome_profile, "Network", "Cookies")
    if not os.path.exists(cookie_db):
        cookie_db = os.path.join(chrome_profile, "Cookies")

    if not os.path.exists(cookie_db):
        print("Chrome cookie database not found.")
        return False

    temporary_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp = temporary_file.name                                   # Chrome 使用同一安全临时文件策略。
    temporary_file.close()
    try:
        shutil.copy2(cookie_db, tmp)
    except PermissionError:
        os.unlink(tmp)
        print("ERROR: Chrome is running. Please close Chrome first.")
        return False

    conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
    cursor = conn.execute(
        "SELECT host_key, name, value, encrypted_value FROM cookies "
        "WHERE host_key LIKE '%bilibili%'"
    )
    rows = cursor.fetchall()
    conn.close()
    os.unlink(tmp)

    return write_netscape_cookie_file(rows, output_path)


if __name__ == "__main__":
    browser = "edge"
    output = "bilibili_cookies.txt"

    for i, arg in enumerate(sys.argv[1:], 1):
        if arg in ("edge", "chrome", "firefox"):
            browser = arg
        elif arg == "--output" and i + 1 < len(sys.argv):
            output = sys.argv[i + 1]
        elif not arg.startswith("--"):
            output = arg

    if browser == "edge":
        success = export_edge_cookies(output)
    elif browser == "chrome":
        success = export_chrome_cookies(output)
    else:
        print(f"Browser '{browser}' not supported yet.")
        success = False

    if success:
        print(f"\nTo use with fetch_bilibili.py:")
        print(f"  python fetch_bilibili.py BVxxx --subtitles --transcribe --cookies {output}")
