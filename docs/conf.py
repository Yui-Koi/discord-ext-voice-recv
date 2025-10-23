# Sphinx configuration for discord-ext-voice-recv
import os
import sys
from datetime import datetime

# Ensure package is importable for autodoc
sys.path.insert(0, os.path.abspath(".."))

project = "discord-ext-voice-recv"
author = "Imayhaveborkedit"
copyright = f"{datetime.now().year}, {author}"

# Use MyST so we can author docs in Markdown
extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "sphinx.ext.doctest",
    "sphinx_copybutton",
    "sphinx_autodoc_typehints",
    "sphinx_design",
]

# MyST and general options
myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "fieldlist",
    "html_image",
    "linkify",
    "substitution",
    "tasklist",
]

# Autodoc / autosummary
autosummary_generate = True
autodoc_typehints = "description"  # put type hints in the description
autodoc_member_order = "bysource"
autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
}

napoleon_google_docstring = False
napoleon_numpy_docstring = True

# Build options
templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

html_theme = "pydata_sphinx_theme"
html_static_path = ["_static"]
html_theme_options = {
    "navigation_depth": 4,
    "show_toc_level": 2,
    "github_url": "https://github.com/Yui-Koi/discord-ext-voice-recv",
    "icon_links": [
        {
            "name": "PyPI",
            "url": "https://pypi.org/project/discord-ext-voice-recv/",
            "icon": "fa-solid fa-box",
        },
        {
            "name": "GitHub",
            "url": "https://github.com/Yui-Koi/discord-ext-voice-recv",
            "icon": "fa-brands fa-github",
        },
        {
            "name": "Discord.py",
            "url": "https://discordpy.readthedocs.io/en/stable/",
            "icon": "fa-solid fa-link",
        },
        {
            "name": "discord.py-self",
            "url": "https://discordpy-self.readthedocs.io/en/latest/",
            "icon": "fa-solid fa-link",
        },
        {
            "name": "Discord Developer Docs",
            "url": "https://discord.com/developers/docs/intro",
            "icon": "fa-solid fa-book",
        },
    ],
}

# Intersphinx: cross-reference external documentation
intersphinx_mapping = {
    "python": ("https://docs.python.org/3", "https://docs.python.org/3/objects.inv"),
    "discordpy": ("https://discordpy.readthedocs.io/en/stable/", "https://discordpy.readthedocs.io/en/stable/objects.inv"),
    "discordpyself": ("https://discordpy-self.readthedocs.io/en/latest/", "https://discordpy-self.readthedocs.io/en/latest/objects.inv"),
}

# Extlinks: quick shortcuts to Discord Developer docs sections
extlinks = {
    "discord_dev": ("https://discord.com/developers/docs/%s", ""),
    "discord_voice": ("https://discord.com/developers/docs/topics/voice-connections#%s", ""),
    "discord_gateway": ("https://discord.com/developers/docs/topics/gateway#%s", ""),
}

# Root document
root_doc = "index"

# Provide substitutions for common links
myst_substitutions = {
    "dpy": "[discord.py](https://discordpy.readthedocs.io/en/stable/)",
    "dpyself": "[discord.py-self](https://discordpy-self.readthedocs.io/en/latest/)",
    "discord_dev_docs": "[Discord Developer Docs](https://discord.com/developers/docs/intro)",
}