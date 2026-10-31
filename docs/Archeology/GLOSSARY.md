# Glossary

**Status:** Proposed on 2026-10-31. The terms below are the ones to use in the UI, code and docs. "Open choices" needs the user's answer.

## The short version

A **model** is one thing you own and would print. It has one ID and one **card**. A model can hold **sub-models**, which are named items you could print on their own. Everything else is a different form of the same thing (a **variant**) or a piece of it (a **part**).

The test for a new item:

1. Would you want to find it, stash it or print it **by itself**? If no, it's a part or a variant.
2. Is it the same object in another scale, support type, hollowing or slicer format? It's a variant.
3. Does it make sense **without** its parent? If yes, it's a model. If it belongs with the parent but stands alone, it's a sub-model.

## Terms

| Term | Means | Example | Stored in |
|---|---|---|---|
| **Source** | Where models come from: MyMiniFactory or a linked library | Loot Studios | `models.source`, `external_libraries` |
| **Release** | A group of models that came out together | August 2022 | `releases` |
| **Model** | One thing you own, with one ID. The unit you browse, search, tag and stash | Hendrik Greenhilt | `models` |
| **Card** | How a model appears on screen. In review, a card is a model that doesn't exist yet | The Lidda Bust tile | Review: `library_candidates`, `library_release_reviews` |
| **Sub-model** | A named item inside a model that you can print and stash by itself | A character inside a bundle archive | `file_archive_submodels` |
| **Variant** | The same object in another form: scale, supported or not, hollow or solid, slicer format | Hendrik at 75mm, supported | Not a row. Read from folder and file names |
| **Part** | A piece of one print that means nothing alone | Base, Torso, Tentacle A | Not a row |
| **File** | One real file on disk | `32mm_Lidda.stl` | `model_files`, `downloaded_files` |
| **Archive** | A file holding other files | `Bust_Bases.zip` | `downloaded_files` |
| **Archive member** | One entry inside an archive | `bases/round.stl` | `file_archive_members` |
| **Path** | A folder or file a card claims in a linked library. The deepest claim wins | `August 2022/Busts/Lidda` | `library_model_folders` |
| **Bust** | A separate model of the same subject, by the user's decision | Hendrik Greenhilt Bust | `models` |
| **Bonus** | A model that came as a reward. It's a model, shown apart | A reward statue | `models.is_bonus` |
| **Stash item** | Something placed in a stash: a model, file, sub-model or archive member | | `stash_items` |

## How they nest

```text
Source: Loot Studios
  Release: August 2022
    Model: Hendrik Greenhilt            one ID, one card
      Variants: 32mm and 75mm; unsupported, Lychee, hollow, solid
        Parts: Base, body
          Files: 32mm_HendrikGreenhilt_Base.stl
    Model: Hendrik Greenhilt Bust       separate, because busts stay separate
      Parts: Head, Sword, Torso
    Model: Watchers Lair
      Parts: Floor1, Pillar3

Source: MyMiniFactory (made-up names)
  Release: 2024-03
    Model: Monthly Bundle               one ID, one card
      Archive: bundle.zip
        Sub-model: Ogre Warrior         found by a peek, can be stashed alone
          Archive members: ogre/body.stl
```

## Collisions to fix

| Word | Problem | Fix |
|---|---|---|
| **Member** | Review cards call their paths `members`, and archives have archive members. Two different things. | Say **path** for a card. Rename `members` in the review code and in saved `cards_json`. |
| **Card** | Means both a catalog tile and a proposed model in review. | Keep one word: a card is how a model looks, and "in review" says it isn't added yet. |
| **Candidate** | Database word for a proposed card. Never shown. | Keep in code only. Never show it in the UI. |
| **Variant** | The resolver's "variant word" list holds any word beside many names, which includes real names like `Throne`. | Keep the meaning above. Call the list **print words** or **learned words** in the code comment pass. |
| **Part** | The resolver's `parts` are repeated names, which is close but not the same as a part of one print. | Fine for now. Note it in the comment pass. |

## Open choices

1. **Linked libraries can't have sub-models yet.** Today a sub-model only comes from an archive peek. `GraveyardMausoleum` inside `Voxshax` is a sub-model by the test above, but the review screen can only make it a separate card. Recommended: the definition stays the same for every source, and review gains **Make Sub-model** after the first version proves itself (E27).
2. **Busts.** They stay separate models, as decided. A bust could instead be a sub-model of its character, but that hides it from browsing.
3. **Sets like Watchers Lair.** Floor and pillar pieces are parts of one model today. If they should be chosen one at a time, they'd be sub-models.
