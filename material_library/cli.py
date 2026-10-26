from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import load_settings
from .library import Library, SearchQuery


def _progress(stage: str, current: int, total: int, message: str) -> None:
    print(f"[{stage}] {current}/{total} {message}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="material_library",
        description="本地 AI 视频素材库：入库、分析、检索、预览",
    )
    parser.add_argument("--config", type=Path, help="配置文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="扫描目录并登记素材")
    scan.add_argument("path", type=Path)
    scan.add_argument("--no-recursive", action="store_true")
    scan.add_argument("--material-type", choices=("普通素材", "成片"), default="普通素材")

    analyze = sub.add_parser("analyze", help="分析待处理素材")
    analyze.add_argument("--limit", type=int, default=None)
    analyze.add_argument("--source-id", type=int, action="append", default=[])
    analyze.add_argument(
        "--reanalyze", action="store_true",
        help="显式重新切片；保留 reviewed/manual 片段",
    )

    query = sub.add_parser("search", help="检索片段")
    query.add_argument("keyword", nargs="?", default="")
    query.add_argument("--tag", action="append", default=[], help="维度:标签，可重复")
    query.add_argument("--min-duration", type=float)
    query.add_argument("--max-duration", type=float)
    query.add_argument("--limit", type=int, default=20)
    query.add_argument("--json", action="store_true")

    sub.add_parser("stats", help="显示统计")
    sub.add_parser("tags", help="显示标签词表")

    serve = sub.add_parser("serve", help="启动本地 Web 服务")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)

    args = parser.parse_args(argv)
    settings = load_settings(args.config)

    if args.command == "serve":
        from .server import serve as run_server

        run_server(args.host, args.port)
        return 0

    library = Library(settings)
    if args.command == "scan":
        stats = library.scan(
            args.path,
            recursive=not args.no_recursive,
            material_type=args.material_type,
            progress=_progress,
        )
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0
    if args.command == "analyze":
        if args.source_id:
            for source_id in args.source_id:
                created = library.analyze_source(source_id, reanalyze=args.reanalyze)
                print(f"source {source_id}: {created} clips")
        elif args.reanalyze:
            ids = library.source_ids_for_reanalyze()
            if args.limit:
                ids = ids[:args.limit]
            stats = {"analyzed": 0, "failed": 0}
            for index, source_id in enumerate(ids, start=1):
                _progress("reanalyze", index, len(ids), str(source_id))
                try:
                    library.analyze_source(source_id, reanalyze=True)
                    stats["analyzed"] += 1
                except Exception as error:
                    stats["failed"] += 1
                    library._mark_source_failed(source_id, str(error))
                    print(f"[reanalyze] failed source {source_id}: {error}")
            print(json.dumps(stats, ensure_ascii=False, indent=2))
        else:
            stats = library.analyze_pending(limit=args.limit, progress=_progress)
            print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0
    if args.command == "search":
        tags_list = []
        for item in args.tag:
            if ":" in item:
                dimension, name = item.split(":", 1)
                tags_list.append((dimension, name))
        result = library.search(SearchQuery(
            keyword=args.keyword, tags=tags_list,
            min_duration=args.min_duration, max_duration=args.max_duration,
            limit=args.limit,
        ))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"共 {result['total']} 条")
            for item in result["items"]:
                print(
                    f"{item['score']:.3f}  {item['id']}  {item['start']:.2f}-"
                    f"{item['end']:.2f}  {item['path']}  {item['description']}"
                )
        return 0
    if args.command == "stats":
        print(json.dumps(library.stats(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "tags":
        print(json.dumps(library.tags_by_dimension(), ensure_ascii=False, indent=2))
        return 0
    return 1
