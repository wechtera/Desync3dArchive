# Glossary

**Status:** Agreed with the user on 2026-10-31. The UI, code and schema don't all match yet. See `CATALOG_HIERARCHY_PLAN.md`.

## The five levels

| Term | Means | Example |
|---|---|---|
| **Collection** | A group of models, sold or stored as one unit. Often one MMF object. In a linked library, one release. | Vampire Court, August 2022 |
| **Model group** | Related models, usually variants. Searchable, and optional. | Ghuls |
| **Model** | One uniquely designed thing. What the user browses. | Ghul A, Hendrik Greenhilt |
| **Artifact** | One printable object, loose or inside an archive. | `32mm_Lidda_Base.stl` |
| **File** | One file on disk, printable or not. | `Vampire Court 3.zip` |

A file and an artifact are different things. A zip is a file and not an artifact. `Ghul A` inside that zip holds artifacts that aren't files on disk.

## Rules

- **Same shape, same model.** 32mm, 75mm, supported, unsupported, hollow and solid all print the same identifiable shape, so they're one model. They are attributes of its artifacts, not levels.
- **A variant is a different shape.** Poses are separate models, joined by a model group.
- **A model can need several artifacts to build.** Base, Torso and Head are artifacts of one model.
- **Busts are separate models.**
- **Each base is its own model,** in a model group called Bases, and tagged `base`.
- **Heroes and Enemies are tags.** They aren't a level.
- **Busts join their character's model group.** They stay separate models.
- **A model group never spans collections automatically.** Linking across collections is a tag's job.
- **The user browses models.** Opening one shows its group. Clicking the group opens a modal of the group's models, and clicking one goes to that model.

## Other words

| Term | Means |
|---|---|
| **Source** | Where a collection comes from: MyMiniFactory or a linked library |
| **Release** | A month's grouping of collections. On a linked library a release and a collection are the same thing |
| **Archive** | A file holding other files |
| **Card** | How a model appears on screen. In review, a model that doesn't exist yet |
| **Path** | A folder or file a review card claims. The deepest claim wins. Replaces `members` in the review code |
| **Bonus** | A collection that came as a reward |
| **Stash item** | Something placed in a stash |

## Tags

Rules agreed on 2026-10-31. Built as E27, separate from stashes.

- Imports create and add tags automatically. An import that brings an existing tag rolls into that tag.
- Tags can also be created by hand, and added to or removed from models and model groups.
- Tags are managed on their own page, a sub-page under Stashes.
- Adding a tag to something only selects an existing tag, with hints while typing, which limits sprawl.
- A tag is one word. Whitespace from a source becomes a dash.

## Old word to new word

| Old | New |
|---|---|
| A `models` row from MMF | Collection |
| A `models` row from a linked library, a review card | Model |
| Sub-model | Model, or model group when it only holds other models |
| Archive member | Artifact if printable. Otherwise an entry in an archive |
| Variant, as used by the resolver for scale and support words | Print attribute |
