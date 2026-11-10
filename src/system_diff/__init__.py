"""system-diff: draws a system from its code, and what each change does to it.

    from system_diff import config, extract, diff, graph
    cfg = config.load(root=Path("."))
    bp = extract.extract(DirSource(Path(".")), cfg)
    svg = graph.system_svg(bp, cfg)
"""
__version__ = "0.5.8"
