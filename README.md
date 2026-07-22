# Rescue Network — animal data refresh

Keeps the adoptable-animal information on rescuenetworkmn.org current: bios,
adoption fees, photos, the animal list, and the Google sitemap. Runs every few
minutes on GitHub's servers, so nobody's computer has to be on.

**This repository cannot change the website itself.** The site's code lives
elsewhere and is deployed by hand after review. The only thing this automation
writes is the animal data record in Cloudflare KV storage.

How it works, briefly:

1. Reads Shelterluv's public animal feed (the same one their adoption widget
   uses) and each animal's public profile page.
2. Assembles one JSON record of every adoptable animal with bio, fee, photos.
3. Writes it to Cloudflare KV — but only when something actually changed.
4. The website's worker serves animal pages and the sitemap from that record.

New or adopted animals are reflected within ~5 minutes; an edited bio or fee
within ~20 minutes. If this automation ever stops, nothing breaks — the site
falls back to the animal data bundled with its last deployment.

Setup lives in one place: the `CLOUDFLARE_API_TOKEN` repository secret
(Settings → Secrets and variables → Actions). Rotate it in Cloudflare and
update it there if it's ever exposed.

`tools/.animals-push-state.json` is the automation's own progress file — the
commits updating it are normal and expected.
