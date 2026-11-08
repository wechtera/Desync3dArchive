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


class FlagTests(unittest.TestCase):
    def flagged(self, folders, nested=None, variants=frozenset({"throne", "32mm"})):
        variants = set(variants) | resolver.PRINT_WORDS
        models = resolver.resolve(folders, variants=variants, parts=set())
        resolver.flag_models(models, variants, set(), nested)
        return {m.name: m.flags for m in models}

    def test_a_guess_stays_a_guess(self):
        folders = ["Objects/Throne_32mm", "Objects/Chest"]
        before = [(m.key, m.folders) for m in resolver.resolve(folders, variants={"throne"}, parts=set())]
        self.flagged(folders)
        self.assertEqual(before, [(m.key, m.folders) for m in resolver.resolve(folders, variants={"throne"}, parts=set())])

    def test_a_leaf_lost_to_the_variant_list_is_flagged(self):
        flags = self.flagged(["Objects/Throne_32mm", "Objects/Chest"])
        self.assertIn("Throne", " ".join(flags["Objects"]))

    def test_a_misspelled_print_word_is_not_a_lost_name(self):
        flags = self.flagged(["Golem/Sollid", "Imp/Solid"], variants={"sollid"})
        self.assertEqual(flags["Golem"], [])

    def test_a_typo_pair_is_flagged_on_both(self):
        flags = self.flagged(["Green Hag/a", "Gree Hag/a"], variants=set("a"))
        self.assertIn("Gree Hag", " ".join(flags["Green Hag"]))
        self.assertIn("Green Hag", " ".join(flags["Gree Hag"]))

    def test_numbered_sets_and_prop_versions_are_not_typos(self):
        flags = self.flagged(["Shadow1", "Shadow2", "PhoenixHammer", "PhoenixHammer Prop"])
        self.assertEqual([f for fs in flags.values() for f in fs], [])

    def test_a_named_item_inside_a_model_is_flagged(self):
        flags = self.flagged(["Voxshax"], {"Voxshax": ["Voxshax/GraveyardMausoleum"]})
        self.assertIn("GraveyardMausoleum", " ".join(flags["Voxshax"]))

    def test_support_folders_inside_a_model_are_not_flagged(self):
        flags = self.flagged(["Mummy"], {"Mummy": ["Mummy/Supported", "Mummy/No Supports"]})
        self.assertEqual(flags["Mummy"], [])


class FileResolutionTests(unittest.TestCase):
    def resolved(self, paths):
        variants = set(resolver.PRINT_WORDS)
        return {m.name: m for m in resolver.resolve_files(paths, variants, resolver.learn_piece_words(paths, variants))}

    def test_poses_are_models_in_a_group(self):
        models = self.resolved(["32mm/Ghouls/32mm_Ghoul_A.stl", "32mm/Ghouls/32mm_Ghoul_B.stl"])
        self.assertEqual(sorted(models), ["Ghoul A", "Ghoul B"])
        self.assertEqual({m.group for m in models.values()}, {"Ghoul"})

    def test_sizes_and_support_are_one_model(self):
        paths = [
            "28mm Supported/Ghouls/28mm_Ghoul_A_Supported.stl", "28mm Unsupported/Ghouls/28mm_Ghoul_A.stl",
            "32mm Unsupported/Ghouls/32mm_Ghoul_A.stl", "32mm Unsupported/Ghouls/32mm_Ghoul_B.stl",
        ]
        self.assertEqual(len(self.resolved(paths)["Ghoul A"].files), 3)

    def test_pieces_build_one_model_wherever_the_word_sits(self):
        models = self.resolved(["Baron/Baron.stl", "Baron/Baron_Base.stl", "Baron/Base_Baron.stl", "Baron/Baron_Head.stl"])
        self.assertEqual(list(models), ["Baron"])
        self.assertEqual(models["Baron"].group, "")

    def test_numbered_pieces_in_a_named_folder_are_one_build(self):
        self.assertEqual(list(self.resolved(["Oak/Oak_P1.stl", "Oak/Oak_P2.stl"])), ["Oak"])

    def test_format_words_do_not_split_a_model(self):
        self.assertEqual(len(self.resolved(["Baron/LYS_Baron.lys", "Baron/Baron.stl"])), 1)


class ResolverFixTests(unittest.TestCase):
    """B2 to B4 from the 2026-11-08 sample. Paths are real ones, trimmed."""

    def resolved(self, paths):
        variants = set(resolver.PRINT_WORDS)
        return {m.name: m for m in resolver.resolve_files(paths, variants, resolver.learn_piece_words(paths, variants))}

    def test_a_kit_folder_never_names_a_group(self):
        # Artisan Guild's groups came out as iarcaneswordstaff, from the folder's slug.
        # No shared start, which is when a group used to fall back to the folder.
        paths = [
            "Apprentice Arcanist A/Kit I (Staff & Spell)/Apprentice_Dagger.stl",
            "Apprentice Arcanist A/Kit I (Staff & Spell)/Arcanist_Apprentice_A_Hood.stl",
        ]
        self.assertEqual({m.group for m in self.resolved(paths).values()}, {"Apprentice Arcanist A"})

    def test_a_group_takes_the_folders_real_words_without_its_catalog_number(self):
        paths = ["727_Deep Sea Valkyries/STL/Deep Sea Valkyries A.stl", "727_Deep Sea Valkyries/STL/Seahorse A.stl"]
        self.assertEqual({m.group for m in self.resolved(paths).values()}, {"Deep Sea Valkyries"})

    def test_pre_supported_folders_are_neither_a_model_nor_a_group(self):
        # Pre stood alone and made Cast n Play's models twice, and a group called pre.
        paths = ["Ants/Pre-Supported/LYS/LYS_Ant_A_Supported.lys", "Ants/STL/Ant_A.stl", "Ants/STL/Ant_B.stl"]
        models = self.resolved(paths)
        self.assertEqual(sorted(models), ["Ant A", "Ant B"])
        self.assertEqual(len(models["Ant A"].files), 2)
        self.assertNotIn("pre", {m.group.lower() for m in models.values()})

    def test_beefed_supports_are_the_same_model(self):
        # Rescale's layout. Before, this made Brass Dragon Stand, Brass Dragon Stand A and ... Bef.
        paths = [
            "BrassDragon_Stand_Supports/Supported/BrassDragon_Stand_Body.stl",
            "BrassDragon_Stand_Supports/Supported/BrassDragon_Stand_TailA.stl",
            "BrassDragon_Stand_Supports/Beefed/BrassDragon_Stand_Body_Bef.stl",
            "BrassDragon_Stand_Supports/Beefed/BrassDragon_Stand_TailA_Bef.stl",
        ]
        self.assertEqual(list(self.resolved(paths)), ["Brass Dragon Stand"])

    def test_a_folder_title_drops_sizes_and_its_catalog_number(self):
        names = set(self.resolved([
            "01_Bandit Khan - 30mm Base/Presupported/STL_Bandit Khan_Supported.stl",
            "Rot_Priest_1_(25mm_Base_41mm_Height)/STL/Rot_Priest_1.stl",
        ]))
        self.assertEqual(names, {"Bandit Khan", "Rot Priest 1"})

    def test_clean_title(self):
        cases = {
            ("01", "Bandit", "Khan", "30mm", "Base"): ["Bandit", "Khan"],
            ("Rot", "Priest", "1", "25mm", "Base", "41mm", "Height"): ["Rot", "Priest", "1"],
            ("Wolpertingers", "1", "2", "25mm", "Base"): ["Wolpertingers", "1", "2"],
            ("Base", "25mm"): ["Base"],
            ("300",): ["300"],
        }
        for words, expected in cases.items():
            with self.subTest(words):
                self.assertEqual(resolver.clean_title(list(words)), expected)

    def test_is_kit(self):
        self.assertTrue(resolver.is_kit(["Kit", "II", "No", "Hood"]))
        self.assertTrue(resolver.is_kit(["Kit", "2"]))
        self.assertFalse(resolver.is_kit(["Kitchen", "Set"]))
        self.assertFalse(resolver.is_kit(["Kit", "Bash"]))

    def test_file_words_tell_one_subject_from_another(self):
        variants = set(resolver.PRINT_WORDS)
        pieces = set(resolver.PIECE_WORDS)
        ant = resolver.file_words("STL/Ant_A.stl", variants, pieces)
        self.assertEqual(ant & resolver.file_words("LYS/LYS_Ant_A_Supported.lys", variants, pieces), {"ant"})
        frog = resolver.file_words("Pose 01/Frog_Rider_Base_Pose01.lys", variants, pieces)
        self.assertFalse(frog & resolver.file_words("Sister and Sister/Sisters_Wraith_Base_Pose01.lys", variants, pieces))
