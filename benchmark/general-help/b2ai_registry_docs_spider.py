import os
import re

import scrapy
from markdownify import markdownify as md
from scrapy.spiders import SitemapSpider

# Env var override for the output directory, so smoke tests can write
# somewhere other than output_markdown/ without touching this file.
OUTPUT_DIR_ENV = "B2AI_DOCS_OUTPUT_DIR"

# MkDocs Material appends this to every <title> on the registry docs site.
TITLE_SUFFIX = " - Bridge2AI Standards Registry"

ASSET_EXT_RE = re.compile(
    r"\.(css|js|json|png|jpe?g|gif|svg|ico|woff2?|ttf|eot|xml|pdf)$", re.IGNORECASE
)


class B2AIRegistryDocsSpider(SitemapSpider):
    """Crawls the Bridge2AI Standards Registry docs site (MkDocs Material,
    https://bridge2ai.github.io/b2ai-standards-registry/) — portal
    process/reference content: standards, data sets, organizations, data
    topics/substrates, use cases, curation and access docs.

    Seeded from the site's sitemap.xml (~189 URLs as of 2026-09-24), which
    lists the correct, working URLs for this site (unlike the
    standards-schemas site's sitemap — see b2ai_schemas_docs_spider.py). Also
    follows in-page links under the same base path as a completeness
    fallback in case the sitemap ever lags behind the live site.
    """

    name = "b2ai_registry_docs"
    allowed_domains = ["bridge2ai.github.io"]
    BASE_URL = "https://bridge2ai.github.io/b2ai-standards-registry/"
    sitemap_urls = [BASE_URL + "sitemap.xml"]

    def __init__(self, *args, output_dir=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Precedence: explicit `-a output_dir=...` spider arg, then the env
        # var, then the same "output_markdown" default the CCKP spiders use.
        self.output_dir = output_dir or os.environ.get(
            OUTPUT_DIR_ENV, "output_markdown"
        )
        self._seen_filenames = set()

    def _in_scope(self, url):
        if not url.startswith(self.BASE_URL):
            return False
        return not ASSET_EXT_RE.search(url.split("#", 1)[0])

    def _slugify_path(self, url):
        # URL path relative to the base, used to dedupe filename collisions
        # and as a fallback name when there's no usable <title>.
        path = url.split("#", 1)[0][len(self.BASE_URL):].strip("/")
        path = path.replace("/", "-")
        return path or "index"

    def _sanitize_title(self, raw_title):
        if not raw_title:
            return None
        title = raw_title.strip()
        if title.endswith(TITLE_SUFFIX):
            title = title[: -len(TITLE_SUFFIX)]
        title = title.strip().replace(" ", "_").replace("/", "-")
        # Drop anything else that's awkward in a filename (colons, etc.).
        title = re.sub(r"[^\w\-]", "", title)
        return title or None

    def parse(self, response):
        raw_title = response.xpath("//title/text()").get()
        title = self._sanitize_title(raw_title)
        path_slug = self._slugify_path(response.url)
        base_name = title or path_slug

        # Extract the MkDocs Material article content, not the nav/sidebar
        # chrome, falling back progressively if the theme structure changes.
        content_html = (
            response.xpath("//article").get()
            or response.css("div.md-content").get()
            or response.xpath("//main").get()
            or response.xpath("//body").get()
        )
        if content_html:
            markdown_content = md(content_html)
            markdown_with_url = f"source_page_url: {response.url}\n\n{markdown_content}"
            os.makedirs(self.output_dir, exist_ok=True)

            filename = f"b2airegistry_{base_name}.md"
            if filename in self._seen_filenames:
                # Collision (e.g. duplicate/near-duplicate titles) — dedupe
                # using the URL path, which is always unique.
                filename = f"b2airegistry_{base_name}__{path_slug}.md"
            self._seen_filenames.add(filename)

            file_path = os.path.join(self.output_dir, filename)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(markdown_with_url)
            self.log(f"Saved file: {file_path}")

        # Fallback: follow in-page links too, restricted to the same base
        # path, in case the sitemap is ever incomplete.
        for href in response.css("a::attr(href)").getall():
            next_page = response.urljoin(href).split("#", 1)[0]
            if self._in_scope(next_page):
                yield scrapy.Request(next_page, callback=self.parse)
