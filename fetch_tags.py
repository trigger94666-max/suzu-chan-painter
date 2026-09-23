#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载中文标签词库（danbooru tag 的中文翻译数据）

数据来源：https://github.com/amenorira/danbooru-tags-data-zh  （MIT License）
为什么不打包进仓库：8.6MB 第三方数据，塞进来会让仓库变胖，而且上游更新时还得重推。
这个脚本从上游拉一份到你本机，目录取 config.json 的 tag_dir。

用法：
  python fetch_tags.py                      # 下到 config.json 里的 tag_dir
  python fetch_tags.py --dir D:/my/tags     # 指定目录
  python fetch_tags.py --force              # 已存在也重下（更新数据用）
  python fetch_tags.py --proxy http://127.0.0.1:7890
"""
import argparse
import os
import sys
import time
import urllib.request

REPO = "amenorira/danbooru-tags-data-zh"
BRANCH = "main"
SUBDIR = "tags"
FILES = ("general.csv", "character.csv", "copyright.csv", "artist.csv", "meta.csv")
RAW = "https://raw.githubusercontent.com/%s/%s/%s/" % (REPO, BRANCH, SUBDIR)


def _download(opener, url, dst, tries=3):
    """分块下载 + 重试。写 .part 再改名——中断时绝不留下半个 CSV 冒充完整文件。"""
    tmp = dst + ".part"
    err = ""
    for i in range(tries):
        try:
            with opener.open(url, timeout=180) as r, open(tmp, "wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
            os.replace(tmp, dst)
            return True, ""
        except Exception as e:
            err = str(e)
            if i < tries - 1:
                time.sleep(1.5 * (i + 1))       # 退避重试：网络抖一下不至于废掉
    try:
        if os.path.isfile(tmp):
            os.remove(tmp)
    except OSError:
        pass
    return False, err


def main():
    ap = argparse.ArgumentParser(description="下载中文标签词库（danbooru tags 中文数据）")
    ap.add_argument("--dir", help="下载到哪个目录（默认取 config.json 的 tag_dir）")
    ap.add_argument("--force", action="store_true", help="已存在也重新下载")
    ap.add_argument("--proxy", help="HTTP 代理，例 http://127.0.0.1:7890")
    a = ap.parse_args()

    out = a.dir
    if not out:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from config import CFG
            out = CFG["tag_dir"]
        except Exception as e:
            print("[x] 读不到 config.json 的 tag_dir（%s）——用 --dir 指定目录" % e)
            return 1
    out = os.path.abspath(out)
    os.makedirs(out, exist_ok=True)
    print("目标目录：%s" % out)
    print("数据来源：https://github.com/%s （MIT License）\n" % REPO)

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": a.proxy, "https": a.proxy})) if a.proxy \
        else urllib.request.build_opener()

    ok = 0
    for fn in FILES:
        dst = os.path.join(out, fn)
        if os.path.isfile(dst) and not a.force:
            print("  跳过  %-14s （已存在；要更新加 --force）" % fn)
            ok += 1
            continue
        good, err = _download(opener, RAW + fn, dst)
        if good:
            print("  完成  %-14s %.1f MB" % (fn, os.path.getsize(dst) / 1048576))
            ok += 1
        else:
            print("  失败  %-14s %s" % (fn, err))

    print("\n%d/%d 个文件就绪。" % (ok, len(FILES)))
    if ok == len(FILES):
        print("启动 server.py 就能用点选面板了。")
    return 0 if ok == len(FILES) else 1


if __name__ == "__main__":
    sys.exit(main())
