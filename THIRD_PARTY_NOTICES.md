# Third-party browser assets

Dockpit's Python runtime uses the standard library. Bundled browser assets have separate licenses:

- Tailwind CSS 3.4.17: MIT, Copyright Tailwind Labs, Inc. The compiled CSS includes Tailwind's license header; the complete license is bundled at `dockpit/static/vendor/tailwind/LICENSE.txt`. Source: https://github.com/tailwindlabs/tailwindcss/tree/v3.4.17
- Font Awesome Free 6.5.1: CSS under MIT, fonts under SIL OFL 1.1, icons under CC BY 4.0. The complete upstream license is bundled at `dockpit/static/vendor/fontawesome/LICENSE.txt`. Source: https://github.com/FortAwesome/Font-Awesome/tree/6.5.1

Tailwind and Font Awesome are served from the local installation, without CDN requests. Playwright is an optional development/CI dependency and is not installed by Dockpit.
