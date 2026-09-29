import json
from cachetools.func import ttl_cache
from zendesk_mcp.auth import TokenExpiredError, api_error_message
from zendesk_mcp.client import ConfigError, get_client
from zendesk_mcp.config import load_config
from zendesk_mcp.errors import ResourceError


def _get_knowledge_base_data() -> str:
    client = get_client()
    sections = list(client.help_center.sections())
    kb = {}
    total_articles = 0
    for section in sections:
        articles = list(client.help_center.sections.articles(section.id))
        total_articles += len(articles)
        kb[section.name] = {
            "section_id": section.id,
            "description": section.description,
            "articles": [{
                "id": a.id,
                "title": a.title,
                "body": a.body,
                "updated_at": str(a.updated_at),
                "url": a.html_url,
            } for a in articles],
        }
    return json.dumps({
        "knowledge_base": kb,
        "metadata": {
            "sections": len(sections),
            "total_articles": total_articles,
        },
    }, indent=2)


@ttl_cache(ttl=3600)
def _get_knowledge_base_data_cached() -> str:
    return _get_knowledge_base_data()


def register_knowledge_base_resource(mcp) -> None:
    cfg = load_config()
    if not cfg.get("knowledge_base_enabled"):
        return

    @mcp.resource("zendesk://knowledge-base", mime_type="application/json", description="Zendesk Help Center articles, grouped by section")
    def knowledge_base() -> str:
        # A failed read is an error with its message, as for the tools. (ttl_cache does not
        # cache an exception, so the next read tries again.)
        try:
            return _get_knowledge_base_data_cached()
        except (ConfigError, TokenExpiredError) as e:
            raise ResourceError(str(e)) from e
        except Exception as e:
            raise ResourceError(api_error_message(e)) from e
