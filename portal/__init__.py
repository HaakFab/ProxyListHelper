"""The proxy portal web application.

Everything in this package runs on the small always/on-demand VPS and never
imports the heavy print pipeline (torch, Pillow, reportlab, numpy, scipy).
It talks to ``proxy_parser.checker`` and ``proxy_parser.moxfield_line`` only --
see ``portal/checking.py`` for the one module that bridges to it.
"""
