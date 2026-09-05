"""Public SEO endpoints for WaziBot.

These endpoints intentionally expose only public, indexable application pages.
They do not require authentication and never enumerate private tenant/business URLs.
"""
from fastapi import APIRouter
from fastapi.responses import Response
from xml.sax.saxutils import escape

router = APIRouter(tags=["seo"])

PUBLIC_URLS = (
    "https://wazibothq.com/",
    "https://wazibothq.com/pricing",
    "https://wazibothq.com/directory",
    "https://wazibothq.com/privacy",
    "https://wazibothq.com/terms",
)


def _sitemap_xml() -> str:
    urls = "".join(f"    <url><loc>{escape(url)}</loc></url>\n" for url in PUBLIC_URLS)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f'{urls}'
        '</urlset>\n'
    )


@router.get("/sitemap.xml", include_in_schema=False)
async def sitemap() -> Response:
    return Response(content=_sitemap_xml(), media_type="application/xml")


@router.get("/robots.txt", include_in_schema=False)
async def robots() -> Response:
    content = (
        "User-agent: *\n"
        "Allow: /\n"
        "\n"
        "Sitemap: https://wazibothq.com/sitemap.xml\n"
    )
    return Response(content=content, media_type="text/plain")
