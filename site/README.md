# TestingHQ site

A small static marketing and docs site for TestingHQ: plain HTML and CSS, no
build step, no server required.

## Preview locally

Double-click `index.html`, or open it directly in a browser:

```
open site/index.html        # macOS
start site/index.html       # Windows
xdg-open site/index.html    # Linux
```

It also works fine served over any static file server if you prefer that
during development, for example:

```
python -m http.server --directory site 8000
```

then visit `http://localhost:8000`.

## Deploying to GitHub Pages

This site has no build step, so Pages can serve it directly from the repo,
no workflow file required:

1. In the repository on GitHub, go to Settings > Pages.
2. Under "Build and deployment", set Source to "Deploy from a branch".
3. Choose the branch that contains this site (for example `main`, once this
   content is merged) and set the folder to `/site`.
4. Save. GitHub will publish `site/index.html` at the resulting Pages URL.

If a folder other than `/site` is required by your Pages setup, copy the
contents of this directory to the repository root or to `/docs` rather than
changing how the site itself is built. Nothing in here depends on a
particular deployment path.

## Files

- `index.html` - the whole site, one page.
- `styles.css` - all styling, plain CSS with a light and dark theme via
  `prefers-color-scheme`.
