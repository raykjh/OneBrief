"""Deterministic preservation of explicit replacement and rebuild intent."""

from __future__ import annotations

import re


_NEW_PRODUCT_SURFACE = re.compile(
    r"(?:"
    r"\b(?:new|newly[ -]?built|fresh|replacement)\s+(?:unity\s+)?"
    r"(?:client|ui|user[ -]?interface|screen|surface|scene|prefab|presentation[ -]?layer)\b"
    r"|\b(?:rebuild|reconstruct|recreate|replace|build[ -]?from[ -]?scratch)\b.{0,40}"
    r"\b(?:client|ui|user[ -]?interface|screen|surface|scene|prefab|presentation[ -]?layer)\b"
    r"|\b(?:new|newly\s+constructed|newly[ -]?built)\s+"
    r"(?:login|lobby|settings)(?:\s+(?:production\s+)?(?:surface|scene|prefab|ui|screen|shell))\b"
    r"|(?:새로운|신규)\s*(?:Unity\s*)?(?:클라이언트|UI|화면|씬|프리팹|표현\s*계층)"
    r"|(?:Unity\s*)?(?:클라이언트|UI|화면|씬|프리팹|표현\s*계층)(?:을|를|은|는)\s*"
    r"(?:새롭게|신규로|새로\s*(?:만들|제작|구축)|재구축|교체)"
    r")",
    re.IGNORECASE | re.DOTALL,
)


def requires_new_product_construction(*values: str) -> bool:
    """Return true only for explicit new/rebuild/replace presentation scope."""

    return bool(_NEW_PRODUCT_SURFACE.search("\n".join(value for value in values if value)))


def construction_directives(*values: str) -> list[str]:
    """Keep the exact user-authored clauses that establish construction intent."""

    directives: list[str] = []
    for value in values:
        for clause in re.split(r"(?<=[.!?])\s+|[\r\n]+", value or ""):
            normalized = clause.strip(" -\t")
            if normalized and _NEW_PRODUCT_SURFACE.search(normalized):
                directives.append(normalized[:500])
    return list(dict.fromkeys(directives))[:12]
