"""Pure regression checks: report rankings do not modify detection decisions."""
import ast
import re
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.subtitle_damage_report import EvidenceRanking, reason_summary, reason_matches, evidence_lines
from app.subtitle_damage_policy import isolated_letter_stats, visible_dialogue, OCR_REASON

node = next(n for n in ast.parse((ROOT / 'app/v51.py').read_text()).body
            if isinstance(n, ast.FunctionDef) and n.name == 'damage_kind')
scope = {'re': re}
exec(compile(ast.Module(body=[node], type_ignores=[]), 'detector', 'exec'), scope)
classify = scope['damage_kind']


def track(index=0, damage='Possible mojibake'):
    return {'path': f'/fixture/{index}.mkv', 'source': 'embedded', 'type_index': 0,
            'external_path': '', 'damage': damage, 'label': f'Movie {index}'}


def subtitle(lines):
    return '\n\n'.join(f'{i}\n00:00:01,000 --> 00:00:02,000\n{line}'
                       for i, line in enumerate(lines, 1))


class DamageReportTests(unittest.TestCase):
    def test_summary_deduplicates_tracks_and_counts_overlapping_reasons(self):
        a = track(damage='Possible mojibake + Replacement characters + Possible mojibake')
        result = reason_summary([{'title': 'Movie', 'streams': [a, a, track(1)]}])
        self.assertEqual(result['stream_count'], 2)
        self.assertEqual([(r['reason'], r['stream_count']) for r in result['reasons']],
                         [('Possible mojibake', 2), ('Replacement characters', 1)])

    def test_real_mojibake_visible_with_context(self):
        text = '1\n00:00:01,000 --> 00:00:02,000\nNÃ£o! NÃ£o!\n'
        self.assertTrue(reason_matches(text, 'Possible mojibake', 'embedded', 'utf-8', classify))
        ranking = EvidenceRanking('Possible mojibake')
        for i in range(5): ranking.add(track(i), text, 'utf-8', ['Possible mojibake'])
        result = ranking.result(7)
        example = result['examples'][0]
        self.assertEqual((example['example'], example['occurrences'], example['stream_count']), ('Ã£', 10, 5))
        self.assertEqual(len(example['locations']), 3)
        self.assertEqual(example['locations'][0]['line'], 3)
        self.assertEqual(example['locations'][0]['cue'], '1')
        self.assertEqual(result['unavailable_streams'], 2)

    def test_controls_not_lost_by_splitting_and_nonutf_is_not_damage_proof(self):
        found = list(evidence_lines('A\x0bB\x0bC\x0cD', 'Control characters'))
        self.assertEqual([(x[0], x[1]) for x in found], [('U+000B', 2), ('U+000C', 1)])
        self.assertIn('\\u000B', found[0][2]['text'])
        self.assertTrue(reason_matches('Olá', 'Non-UTF-8 source bytes', 'embedded', 'cp1252 inferred', classify))
        self.assertFalse(reason_matches('Olá', 'Non-UTF-8 source bytes', 'external', 'cp1252 inferred', classify))

    def test_top30_and_unreproduced(self):
        ranking = EvidenceRanking('Possible mojibake')
        text = ' '.join('Ã' + chr(0x80 + i) for i in range(40))
        ranking.add(track(), text, '', ['Possible mojibake'])
        ranking.add(track(1), 'Correct text', '', [])
        result = ranking.result(3)
        self.assertEqual(len(result['examples']), 30)
        self.assertEqual(result['distinct_examples'], 40)
        self.assertEqual(result['unreproduced_count'], 1)
        self.assertEqual(result['unavailable_streams'], 1)

    def test_encoding_bulk_count_covers_all_media_not_example_locations(self):
        ranking = EvidenceRanking('Non-UTF-8 source bytes')
        for i in range(40):
            ranking.add(dict(track(i), codec='subrip'), subtitle(['Café amanhã.']),
                        'Windows-1252 (inferred)', ['Non-UTF-8 source bytes'])
        ranking.add(dict(track(40), codec='ass'), subtitle(['Café amanhã.']),
                    'Windows-1252 (inferred)', ['Non-UTF-8 source bytes'])
        result = ranking.result(41)['examples'][0]
        self.assertEqual(result['quickfix_media_count'], 40)
        self.assertEqual(result['media_count'], 41)
        self.assertEqual(len(result['locations']), 3)

    def test_simple_rules_agree_with_inspector(self):
        for text in ['', 'Olá, tudo bem?', 'NÃO', 'Broken Ã£', 'Bad �', '1\nInvalid timing\nabc']:
            for reason in ['Possible mojibake', 'Replacement characters', 'Empty subtitle track (no cues)']:
                self.assertEqual(reason_matches(text, reason, 'embedded', '', classify), reason in classify(text))

    def test_normal_common_language_text_and_punctuation_are_allowed(self):
        for text in ['NÃO! SÃO PAULO. AMANHÃ.', 'Ângela, mantenha o ÂNIMO.',
                     '“Olá…” — disse João. «Está bem!»', 'Don’t worry—it’s fine…',
                     '¿Qué pasó? ¡Mañana! Français: âge, âme, Noël.',
                     '“…” — «?!» ' * 20, 'NA\u0303O!']:
            self.assertEqual(classify(text), 'None', text)

    def test_corruption_not_hidden_by_legitimate_portuguese(self):
        for text in ['NÃO! CoraÃ§Ã£o.', 'Ângela: cafÃ©.', 'Donâ€™t go.', 'OlÃ¡', 'Â©']:
            self.assertIn('Possible mojibake', classify(text), text)
        self.assertIn('Control characters', classify('NÃO! \x0e [Music]'))
        self.assertIn('Replacement characters', classify('“Olá” �'))
        self.assertIn('Empty subtitle track', classify(''))

    def test_italic_song_tracks_are_not_ocr_gibberish(self):
        # The previous rule counted both italic tag names as single letters.
        # Repeated short sung lines reproduce Road to Morocco / Same Time,
        # Next Year without storing copyrighted complete movie subtitles.
        for lines in [['<i>Olá.</i>'] * 12, ['<i>Paixão e emoção!</i>'] * 12,
                      ['<i>O luar torna-se você</i>'] * 12]:
            text = subtitle(lines)
            stats = isolated_letter_stats(text)
            self.assertEqual(stats.fragmented, 0)
            self.assertNotIn(OCR_REASON, classify(text))
            self.assertEqual(list(evidence_lines(text, OCR_REASON)), [])

    def test_jinxed_credit_time_units_do_not_become_isolated_letters(self):
        text = subtitle(['leg. para remendos rmz BRRIP\n43m32s / 1h07m47s / 1h22m43s'])
        stats = isolated_letter_stats(text)
        self.assertEqual((stats.words, stats.isolated, stats.fragmented), (5, 0, 0))
        self.assertEqual(classify(text), 'None')
        self.assertEqual(list(evidence_lines(text, OCR_REASON)), [])

    def test_formatting_and_entities_only_analyzed_as_visible_text(self):
        line = '<font color="#ffff00"><i>{\\i1}Ol&#225;.</i></font><br/>Tudo bem?'
        self.assertEqual(visible_dialogue(line), 'Olá.\nTudo bem?')
        self.assertEqual(visible_dialogue('<John> está aqui.'), '<John> está aqui.')
        self.assertEqual(visible_dialogue('NA\u0303O'), 'NÃO')
        self.assertEqual(classify(subtitle([line] * 12)), 'None')
        self.assertNotIn(OCR_REASON, classify(subtitle(['<u>E é o a e o i y.</u>'] * 12)))

    def test_normal_grammar_contractions_initials_and_links_are_not_fragments(self):
        for line in ['E é o que é.', "I don't know.", "(à l'aube ?)", "« d'où ? »", "C’est l’été.",
                     'A. B. C. D. E.', 'S-O-S S-O-S',
                     'https://www.r.m.z.invalid/43m32s', '1h07m47s 2m30s 99s',
                     'A1 B2 C3 D4 E5 F6', 'X_Y_Z_X_Y_Z']:
            text = subtitle([line] * 12)
            self.assertEqual(isolated_letter_stats(text).fragmented, 0, line)
            self.assertNotIn(OCR_REASON, classify(text), line)
            self.assertEqual(list(evidence_lines(text, OCR_REASON)), [], line)

    def test_real_ocr_fragments_still_detected_and_examples_are_relevant(self):
        text = subtitle(['<i>Olá.</i>', 'a b c d e f g h i j k l', 'E é o que é.'])
        stats = isolated_letter_stats(text)
        self.assertTrue(stats.suspicious)
        self.assertEqual(stats.fragmented, 12)
        self.assertIn(OCR_REASON, classify(text))
        self.assertTrue(reason_matches(text, OCR_REASON, 'embedded', 'UTF-8', classify))
        examples = list(evidence_lines(text, OCR_REASON))
        self.assertEqual([example[0] for example in examples], ['a b c d e f g h i j k l'])
        self.assertEqual((examples[0][2]['cue'], examples[0][2]['line']), ('2', 7))
        divided = subtitle(['<i>t e x t o</i>', 'q u e b r a d o'])
        self.assertTrue(isolated_letter_stats(divided).suspicious)
        self.assertIn(OCR_REASON, classify(divided))

    def test_ocr_refresh_does_not_suppress_other_damage(self):
        text = subtitle(['<i>Olá.</i>'] * 12) + '\nCafÃ©. � \x0b'
        result = classify(text)
        self.assertNotIn(OCR_REASON, result)
        for reason in ['Possible mojibake', 'Replacement characters', 'Control characters']:
            self.assertIn(reason, result)

    def test_punctuation_separated_ocr_stays_reported(self):
        # Real cached 24 tracks also contain isolated letters mixed with many
        # symbols rather than long whitespace-separated runs. Do not hide
        # this form of OCR damage while correcting the italic-song false hit.
        text = subtitle(['b!c!d!f!g!h!j!k!l!m!n!p'])
        stats = isolated_letter_stats(text)
        self.assertEqual(stats.fragmented, 0)
        self.assertGreaterEqual(stats.unusual, 8)
        self.assertTrue(stats.suspicious)
        self.assertIn(OCR_REASON, classify(text))
        self.assertEqual(len(list(evidence_lines(text, OCR_REASON))), 1)
        initials = subtitle(['A. B. C. D. E. F. G. H. I. J. K. L.'])
        self.assertGreater(isolated_letter_stats(initials).symbol_ratio, .25)
        self.assertFalse(isolated_letter_stats(initials).suspicious)
        # Contractions slightly lower a real garbled track's token ratio;
        # dense symbols are corroboration, unlike italic markup or grammar.
        noisy = subtitle(['b!c!d!f!g!h!j!k ' + 'word ' * 16 + ' !@$%&=^|' * 4])
        score = isolated_letter_stats(noisy)
        self.assertTrue(.30 <= score.ratio < .35)
        self.assertTrue(score.suspicious)

    def test_uncased_one_character_words_not_treated_as_ocr_letters(self):
        text = subtitle(['我 你 他 她 它 们 吗 呢 啊 我 你 他'] * 3)
        self.assertFalse(isolated_letter_stats(text).suspicious)
        self.assertNotIn(OCR_REASON, classify(text))


if __name__ == '__main__': unittest.main()
