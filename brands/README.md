# Brand assets

Home Assistant does not load an integration's icon from the integration itself —
the frontend fetches it from `brands.home-assistant.io`. These files are the
copies submitted to [home-assistant/brands][brands], where they live under
`custom_integrations/proof_plus/`. Until that submission is merged, Home
Assistant shows the default placeholder.

| File | Size |
|---|---|
| `proof_plus/icon.png` | 256x256 |
| `proof_plus/icon@2x.png` | 512x512 |

Both are PNG with no transparent padding, as the brands repository requires.
The artwork is original to this project and is not IME's or Proof's logo.

[brands]: https://github.com/home-assistant/brands
