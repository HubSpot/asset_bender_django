from django import template
from asset_bender.bundling import get_static_url, get_static_build_version

register = template.Library()

@register.simple_tag(takes_context=True)
def bender_url(context, full_asset_path):
    return get_static_url(full_asset_path, template_context=context)

@register.simple_tag(takes_context=True)
def bender_build_for(context, project_name):
    return get_static_build_version(project_name, template_context=context)

# Deprecated
@register.simple_tag
def static_url(static_path):
    return get_static_url(static_path)

@register.simple_tag(takes_context=True)
def static3_url(context, full_asset_path):
    return get_static_url(full_asset_path, template_context=context)
