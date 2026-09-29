# Bridge2AI Standards Explorer Copilot Docs

Hugo + [hugo-book](https://themes.gohugo.io/themes/hugo-book/) docs site.

## Local development

```sh
git clone --recurse-submodules https://github.com/Bankso/b2ai-se-chatbot.git
cd b2ai-se-chatbot/docs
hugo server
```

If you already cloned without `--recurse-submodules`, fetch the theme with:

```sh
git submodule update --init --recursive
```

## Build

```sh
cd docs
hugo --minify   # outputs to docs/public/, not committed
```

## CI / GitHub Pages

`.github/workflows/deploy-docs.yml` checks out the repo (with submodules), installs Hugo, and builds. The site is served at `https://bankso.github.io/b2ai-se-chatbot/`, hence `baseURL` in `hugo.toml`.
