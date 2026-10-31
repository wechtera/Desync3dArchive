"""The resolver knows no library. These trees are unlike Loot on purpose."""

import unittest

from desync_archival.services import model_resolver as resolver


def names(models):
    return sorted(m.name for m in models)


class ResolverTests(unittest.TestCase):
    def test_variants_collapse_into_one_model(self):
        folders = [f"Dragon/{scale}/{support}" for scale in ("32mm", "75mm") for support in ("Supported", "No Supports")]
        models = resolver.resolve(folders)
        self.assertEqual(names(models), ["Dragon"])
        self.assertEqual(len(models[0].folders), 4)

    def test_a_different_layout_with_the_character_deepest(self):
        folders = [f"Heavy Units/{v}/Stone Giant_{v}" for v in ("Supported", "Unsupported")]
        folders += [f"Heavy Units/{v}/Ogre_{v}" for v in ("Supported", "Unsupported")]
        self.assertEqual(names(resolver.resolve(folders)), ["Ogre", "Stone Giant"])

    def test_learned_words_need_no_list(self):
        folders = [f"Set{i}/Hero{i}_GloomPrint" for i in range(40)]
        self.assertIn("gloomprint", resolver.learn_variant_words(folders))

    def test_a_character_is_not_learned_as_a_variant(self):
        folders = [f"Set{i}/Hero{i}_GloomPrint" for i in range(40)] + ["Set1/Bull_32mm", "Set2/Bull_75mm"]
        self.assertNotIn("bull", resolver.learn_variant_words(folders))

    def test_a_well_covered_character_in_a_small_library_stays_a_character(self):
        folders = [f"{s}mm/{v}/BrazenBull_{s}mm_{v}" for s in (28, 32, 54, 75) for v in ("Lychee", "Solid", "Hollow")]
        self.assertNotIn("brazenbull", resolver.learn_variant_words(folders))
        self.assertEqual(names(resolver.resolve(folders)), ["Brazen Bull"])

    def test_typos_of_known_words_are_caught(self):
        self.assertIn("nosuports", resolver.learn_variant_words(["A/NoSuports", "B/Dragon"]))

    def test_short_real_words_are_not_typos(self):
        self.assertNotIn("sold", resolver.learn_variant_words(["A/Sold", "B/Dragon"]))

    def test_busts_stay_separate(self):
        self.assertEqual(names(resolver.resolve(["Dragon_32mm", "Dragon_Bust"])), ["Dragon", "Dragon Bust"])

    def test_parts_roll_up_to_their_subject(self):
        folders = [f"{who}/{part}/Supported" for who in ("Ogre", "Troll", "Giant") for part in ("Base", "Body")]
        self.assertEqual(names(resolver.resolve(folders)), ["Giant", "Ogre", "Troll"])

    def test_nothing_usable_is_flagged_for_review(self):
        models = resolver.resolve(["32mm/Supported"])
        self.assertEqual(models[0].confidence, "low")
        self.assertEqual(models[0].name, "Untitled")

    def test_the_category_survives_learning(self):
        model = resolver.resolve(["Raiders_32mm_Supported/Ogre_32mm_Supported"])[0]
        self.assertEqual(model.category, "Raiders")
