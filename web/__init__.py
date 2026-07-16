"""TestingHQ Blast web UI: branded single-page app + thin stdlib server.

Vanilla HTML/CSS/JS on the front end, `http.server` on the back end, and a
single adapter seam (`web/adapter.py`) standing in for the Blast engine
until the parallel engine lane lands.
"""
