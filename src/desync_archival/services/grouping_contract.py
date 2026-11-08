"""The contract between the app and any LLM: what an answer must look like, and the words that
explain it. The JSON Schema in each export is generated from these models, so what the LLM is
told and what the import checks can't drift apart.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "1"
NAME_MAX = 120
REASON_MAX = 500
TAGS_PER_MODEL = 20
ALTERNATES_MAX = 3

Confidence = Literal["high", "medium", "low"]


class _Lenient(BaseModel):
    # Extra fields an LLM adds, such as notes, are read past. Fields that would change who or what
    # a unit is are caught before this, by the identity check in the import.
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class AnswerModel(_Lenient):
    name: str = Field(min_length=1, max_length=NAME_MAX, description="What a person would call this model")
    group: str | None = Field(default=None, max_length=NAME_MAX, description="Its model group's name, or null")
    artifacts: list[str] = Field(min_length=1, description="The a-IDs of every printable that builds or prints this model")
    image: str | None = Field(default=None, description="An i-ID from this unit, \"cover\", or null")
    image_alternates: list[str] = Field(default_factory=list, max_length=ALTERNATES_MAX, description="Up to three more i-IDs, best first")
    image_shared: bool = Field(default=False, description="True when this picture also shows other models, such as a group shot")
    image_confidence: Confidence = "low"
    image_reason: str = Field(default="", max_length=REASON_MAX)
    render_artifact: str | None = Field(default=None, description="The a-ID that best shows this model if a render is made")
    tags: list[str] = Field(default_factory=list, max_length=TAGS_PER_MODEL, description="Only from tags.txt, and only where tags_allowed is true")
    confidence: Confidence
    reason: str = Field(default="", max_length=REASON_MAX, description="Why, whenever confidence is below high")


class AnswerGroup(_Lenient):
    name: str = Field(min_length=1, max_length=NAME_MAX)
    image: str | None = Field(default=None, description="An i-ID from this unit, \"cover\", or null")
    image_confidence: Confidence = "low"
    image_reason: str = Field(default="", max_length=REASON_MAX)


class Unplaced(_Lenient):
    id: str = Field(description="An a-ID that isn't part of any model")
    reason: str = Field(default="", max_length=REASON_MAX)


class AnswerUnit(_Lenient):
    unit_id: str = Field(description="Copied exactly from the batch")
    fingerprint: str = Field(description="Copied exactly from the batch")
    confidence: Confidence
    reason: str = Field(default="", max_length=REASON_MAX)
    models: list[AnswerModel] = Field(min_length=1)
    groups: list[AnswerGroup] = Field(default_factory=list)
    unplaced: list[Unplaced] = Field(default_factory=list)
    suggested_tags: list[str] = Field(default_factory=list, max_length=TAGS_PER_MODEL)


class Answer(_Lenient):
    export_id: str = Field(description="Copied exactly from the batch")
    units: list[AnswerUnit] = Field(min_length=1)


def answer_schema() -> dict:
    schema = Answer.model_json_schema()
    schema["title"] = "Grouping answer"
    schema["$comment"] = f"Contract version {CONTRACT_VERSION}. Return one JSON object like this and nothing else."
    return schema


README = """\
Grouping by LLM: what to do with this zip
=========================================

1. Open a new chat with any LLM.
2. Give it prompt.md, answer-format.json and one batch file, such as batch-01.json.
   If tags.txt is here, give it that too.
3. Ask it to follow prompt.md. It should reply with one JSON object and nothing else.
4. Save the reply as a .json file, for example answer-01.json.
5. Repeat for each batch. A new chat for each batch keeps them from mixing.
6. In the app, open Manage / Retrieve / Reconcile Assets, then LLM Grouping, and upload the answers.

The app checks every answer before anything changes, and shows what would change first.
If an answer has problems, the app writes a message you can paste back to the LLM so it can
fix them. Nothing is applied until you confirm, and every import can be undone.
"""

PROMPT = """\
# Group a 3D printing library by file names

You are helping organise a library of 3D printable miniatures. Each unit in the batch is one
collection: the files a creator released together, from inside its archives or from one release
folder of a linked library. Group each unit's printable files into models, give each model a
name, and choose the best picture for each model.

Judge only by the file and folder names you are given. You cannot see the meshes, the pictures,
or any web page.

Paths start inside the unit's archive when `archives` lists one, and with the archive's name when
it lists several. A linked library's paths start inside its release folder.

## The levels

- A **collection** holds **model groups**, which hold **models**, which are built from **artifacts**.
- An artifact is one printable file. In the batch, each has a short ID: `a1`, `a2` and so on.
- **Same shape, same model.** One model keeps every size, support style and file format: 28mm and
  32mm, supported and unsupported, hollow and solid, and `.stl`, `.lys`, `.chitubox` or `.ctb`.
- **Parts of one build are one model.** A torso, arms, a head, wings, tail pieces and P1 to P10
  build one model.
- **A figure's own options are part of it.** Alternate arms, hands, weapons, heads with and
  without a hood, and its own base all belong to that figure's model.
- **Poses are separate models,** joined by a model group. Ghul A to Ghul E are five models in a
  group called Ghuls.
- **Busts are separate models,** in their character's group.
- **Each base sold on its own is its own model,** in a group called Bases, even when many come in
  one pack: `Round_25_ver_1` to `Round_25_ver_5` are five models, and so is each other shape and
  size. Name each by shape, size and version, as in "Round 25, Version 2". A figure's own base is
  not a model of its own.
- **A pack of swap parts shared by a whole unit** is one model of its own, in that unit's group.
- **Presupported files sometimes call a figure by another name,** such as Drakzor Fangbearer for
  the Lizardman Elite. When folder names pair them, they are one model.
- **Parallel format folders** such as Lychee, STL and Support STL hold one model's files, never
  a second model.

Words that only describe a print, never a new model: sizes like 28mm or 75mm, supported,
unsupported, presupported, pre-supported, supports, beefed, hollow, hollowed, solid, lychee,
chitubox, lys, ctb, stl, files, scale.

## Names

Give each model a name a person would use, such as "Ghul A" or "Captain Salveda". Leave sizes,
base sizes and catalog numbers out of names, except for a base sold on its own, where the size is
its name. Where file names are bare, such as `pose1` or
`weapon1`, build the name from the collection's, as in "Ogre Boss, Pose 1". Group names follow the
same rules.

## Worked examples

Vampire Court has `28mm Supported/Ghuls/28mm_Ghul_A_Supported.stl` and
`32mm Unsupported/Ghuls/32mm_Ghul_A.stl`. Both are the model "Ghul A", in the group "Ghuls".
Ghul B to Ghul E are four more models in that group. `Court Knights` holds `Knight_Banner`,
`Knight_Champion`, `Horse_1` and `Horse_2`: each knight is a model in a group "Court Knights",
and each horse is a part of the knight that rides it when the names pair them, or a model of its
own when they don't.

The Second Face mask has `Mask`, `Mask_L1` to `Mask_L6` and `Mask_R1` to `Mask_R6`. That is one
model, "The Second Face", because the L and R pieces build it.

## Pictures

Pictures matter most, because they are what a person sees first. Each image has a short ID, `i1`,
`i2` and so on, with its path, its size in bytes, its pixel size when known, and role hints read
from its name. The hints can be wrong.

1. Prefer an image whose name matches the model or its variant letter. Folder names count too.
   Names rarely match exactly: allow typos (Swordman and Swordsman), changed prefixes
   (ExoLindworm for XenoLindworm), another name for the figure (AegonWolfKnight for
   Aegon_Werewolf), unit letters (`0SQ Unit A` for Hunter A when `UNIT 1 - Hunters` names them)
   and abbreviations. Lower `image_confidence` when only the name links them.
2. Prefer a single model over a group shot, a full figure over a close-up, a front view over a
   rear or side view, and a finished render over a WIP, a scale sheet or a stat card. If a
   scale sheet or stat card is the only image of that model, use it.
3. Prefer a larger image to a thumbnail.
4. Give each model group its group shot, if there is one.
5. Use a group shot for a model only when no single image fits, and set `image_shared` to true.
   Any picture used by two or more models must have `image_shared` true.
6. Never use an image from another unit.
7. Choose `"cover"` or null rather than guess. A wrong picture is worse than none.
8. When a model has no good image, set `render_artifact` to the a-ID that best shows it: the
   one-piece or full version over a split kit, the main body over a base or an arm, and
   unsupported over supported.

## Tags

Only when a unit has `"tags_allowed": true`, you may tag models, and only with tags from
tags.txt. Put any other tag you would like in `suggested_tags`. A person approves those.

## What to return

Return one JSON object that matches answer-format.json, and nothing else: no prose, no
markdown. Copy `export_id`, and each unit's `unit_id` and `fingerprint`, exactly from the batch.

- Every a-ID in a unit's `printables` appears exactly once: in one model's `artifacts`, or in
  `unplaced` with a reason. Use `unplaced` only for files that are not part of any model, such as
  a test print or a support tester.
- Only use IDs that unit lists. Never invent an ID, and never move an ID to another unit.
- Never add fields that describe the unit itself, such as a creator, collection, path or size.
  The app already knows those, and an answer that sets them is refused.
- `current` shows how the app groups the unit today. It is only a hint, and it is often wrong.

## Confidence

Rate the unit, each model and each picture as `high`, `medium` or `low`, and give a reason for
anything below high. Rate low when unsure: a unit rated high applies without a person checking.

The unit's and each model's confidence rate the grouping only: which files go together and what
they are called. Doubt about a picture goes in `image_confidence` alone. A shared group shot, a
scale sheet or no picture at all never lowers a model's or the unit's confidence.
"""

# One paragraph: a person copies it into a chat as is, so line breaks would land mid-sentence.
FIX_INTRO = (
    "The app checked your answer and found problems. Fix only what is listed and keep everything else "
    "about those units as it was. Then send just these units again: one JSON object matching "
    "answer-format.json, with export_id {export_id}, holding only the units listed below. Nothing else.\n"
)
