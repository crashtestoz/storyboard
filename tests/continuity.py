"""Continuity and cut integration checks; no model inference. Run from storyboard."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import assemble
from server.backends.base import ShotPaths
from server.backends.vpipe_backend import _effective_video_model, _ref2va_references, _resolved_prompt, _has_downstream_chain
from server.orchestrator import ShotRun
from server.store import render_fingerprint, speech_fingerprint
from server.speech import prepare_recording, engine_fingerprint
from render_all import a_board, an_orchestrator, make_clip


class Continuity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.orch = an_orchestrator(self.root)
        self.board = a_board()
        for i in (1, 2):
            self.board['shots'][i]['startRef'] = {'kind':'chain', 'from':f's{i}'}
        self.slug = 'test'
        self.orch.store.save(self.slug, self.board)

    def frame(self, scene=1, content=b'current frame'):
        p = self.root / f'test/shots/{scene:02}/frames/frame-0000.png'
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def test_reference_manifest_retains_identity_and_voice(self):
        shot = self.board['shots'][1]
        shot.update(dialogueSource='native', dialogue='Hello', characterIds=['frog'])
        shot['startRef']['resolved'] = 'previous.png'
        self.board['characters'] = [dict(id='frog', name='Kermit', image={'path':'portrait.png'}, voice={'path':'voice.wav'})]
        self.assertEqual(_effective_video_model(shot, self.board)[0], 'ref2va')
        paths = ShotPaths(self.root, self.root/'shot', 'shot', self.root)
        refs = _ref2va_references(shot, self.board, paths)
        self.assertEqual([Path(r).name for r in refs], ['previous.png','portrait.png','voice.wav'])
        prompt = _resolved_prompt(shot,self.board,model='ref2va')
        self.assertIn('<Picture 1>: Previous scene', prompt)
        self.assertIn('<Picture 2>: Kermit', prompt)
        self.assertIn('speaks aloud', prompt)
        self.assertTrue(_has_downstream_chain(self.board['shots'][0], self.board))
        # Overwriting a chain-sourced Start frame with a manually chosen one
        # is just an ordinary edit now -- there is no second field left to
        # conflict with, so this no longer raises.
        shot['startRef'] = {'path': 'anchor.png'}
        self.assertEqual(_effective_video_model(shot, self.board)[0], 'ref2va')

    def test_same_filename_changed_content_invalidates(self):
        p = self.frame()
        board = self.orch.store.load(self.slug)
        shot = board['shots'][1]
        original = render_fingerprint(shot,board)
        self.assertEqual(self.orch._resolve_chain(shot,board['shots'],self.slug),None)
        self.assertEqual(original, render_fingerprint(shot,board))
        p.write_bytes(b'new frame at identical filename')
        updated = self.orch.store.load(self.slug)
        self.assertNotEqual(original, render_fingerprint(updated['shots'][1],updated))

    def test_downstream_cascade_and_failed_source(self):
        for s in self.board['shots']: s['renderFingerprint']=render_fingerprint(s,self.board)
        self.board['shots'][0]['prompt'] += ' Changed'
        pending,_ = self.orch._pending(self.board)
        self.assertEqual([s['id'] for s in pending], ['s1','s2','s3'])
        self.frame()
        self.orch._runs['s1'] = ShotRun(shot_id='s1', status='failed')
        self.assertIn('not completed', self.orch._resolve_chain(self.board['shots'][1],self.board['shots'],self.slug))

    def test_selected_scene_includes_missing_prerequisites(self):
        self.orch._prime_batch(self.slug,['s3'])
        self.assertEqual(self.orch._order,['s1','s2','s3'])
        self.assertIn('required',self.orch._queued_because['s1'])

    def test_cycles_and_missing_sources_rejected_before_queue(self):
        for source in ['s3','missing']:
            self.board['shots'][0]['startRef']={'kind':'chain','from':source}
            self.orch.store.save(self.slug,self.board)
            with self.assertRaisesRegex(ValueError,'earlier scene'):
                self.orch._prime_batch(self.slug,None)

    def test_speech_failure_prevents_video_jobs(self):
        self.orch._prime_batch(self.slug,None)
        self.orch.prepare_dialogue=lambda slug,sid: (_ for _ in ()).throw(ValueError('voice offline'))
        with patch.object(self.orch,'_run_one') as render:
            self.orch._run_batch(self.slug)
            render.assert_not_called()
        self.assertIn('voice offline',self.orch._error)
        self.assertEqual(self.orch.store.load(self.slug)['shots'][0]['status'],'blocked')

    def test_current_speech_reused_changed_voice_regenerated_native_skipped(self):
        board = self.board
        shot = board['shots'][0]
        shot.update(dialogue='Hello', dialogueSpokenText='Hello',dialogueSpokenStyle='',dialogueSource='recording')
        engine = SimpleNamespace(id='fake',supports_cloning=True,health=lambda:(True,''))
        shot['speechFingerprint']=speech_fingerprint(shot,board)
        shot['speechEngineFingerprint']=engine_fingerprint(engine)
        self.orch.store.save(self.slug,board)
        folder=self.root/'test/shots/01';folder.mkdir(parents=True)
        (folder/'dialogue.wav').write_bytes(b'a'*2048)
        ctx=SimpleNamespace(store=self.orch.store,data_dir=self.root,tts=lambda kind:engine)
        with patch('server.speech.generate_take',return_value={}) as generate:
            prepare_recording(ctx,self.slug,'s1');generate.assert_not_called()
            board['shots'][0]['dialogueVoice']='different'
            self.orch.store.save(self.slug,board)
            prepare_recording(ctx,self.slug,'s1');generate.assert_called_once()
            generate.reset_mock()
            board['characters']=[dict(id='c',voice={'path':'voice.wav'},image={'path':'portrait.png'})]
            shot.update(characterIds=['c'],dialogueSource='native')
            self.orch.store.save(self.slug,board)
            prepare_recording(ctx,self.slug,'s1');generate.assert_not_called()

    def test_continuity_ref_migrates_into_start_ref(self):
        # continuityRef is retired: Store.migrate() folds it into startRef so
        # old boards keep their continuity settings under the single merged
        # field.
        board = a_board()
        board['shots'][0]['continuityRef'] = {'kind': 'chain', 'from': 's0', 'mode': 'reference'}
        migrated = self.orch.store.migrate(board)
        shot = migrated['shots'][0]
        self.assertNotIn('continuityRef', shot)
        self.assertEqual(shot['startRef'], {'kind': 'chain', 'from': 's0'})

        # A shot with both set (shouldn't occur today -- _effective_video_model
        # has always rejected the combination -- but boards are hand-editable)
        # keeps the more specific startRef and drops the orphaned continuity ref.
        board = a_board()
        board['shots'][0]['continuityRef'] = {'kind': 'chain', 'from': 's0'}
        board['shots'][0]['startRef'] = {'path': 'manual.png'}
        migrated = self.orch.store.migrate(board)
        shot = migrated['shots'][0]
        self.assertNotIn('continuityRef', shot)
        self.assertEqual(shot['startRef'], {'path': 'manual.png'})

    def test_native_dialogue_with_no_voice_is_not_blocking(self):
        # Dialogue with no assigned character (or no voice clip) used to
        # hard-fail. It's now a supported case: H3 is still asked to voice
        # the line, in a voice it judges fits the character and scene,
        # rather than staying silent for a separate recording.
        board = self.board
        shot = board['shots'][0]
        shot.update(dialogue='Hello', characterIds=[], dialogueSource='native')
        self.orch.store.save(self.slug, board)
        ctx = SimpleNamespace(store=self.orch.store, data_dir=self.root, tts=lambda kind: None)
        prepare_recording(ctx, self.slug, 's1')  # must not raise

        prompt = _resolved_prompt(shot, board, model='ref2va')
        self.assertIn('in a voice that fits the character and scene', prompt)
        self.assertNotIn('natural jaw and lip movement', prompt)
        self.assertNotIn('dubbed separately', prompt)
        self.assertIn('Dialogue priority:', prompt)

    def test_native_dialogue_with_no_voice_does_not_relay_a_stale_dub(self):
        # Regression guard for the voiceClonedNatively -> nativeDialogueSpoken
        # rename: a shot switched to native dialogue with no voice clip must
        # still be treated as "H3 already spoke this line," so a leftover
        # dialogue.wav from a prior recording-based setting is never muxed on
        # top of a clip that already speaks the line (the "second voice" bug).
        from server.backends.base import JobSpec, RunResult, Validation
        board = self.board
        shot = board['shots'][0]
        shot.update(dialogue='Hello', characterIds=[], dialogueSource='native')
        self.orch.store.save(self.slug, board)
        folder = self.root / 'test/shots/01'
        folder.mkdir(parents=True)
        (folder / 'dialogue.wav').write_bytes(b'a' * 2048)  # a stale prior recording

        class FakeBackend:
            def prepare(self, shot, project, paths):
                return JobSpec(shot_id='s1', payload={'nativeDialogueSpoken': True})

            def run(self, spec, on_event, should_cancel):
                return RunResult(exit_code=0, started_at=0.0, ended_at=1.0)

            def validate(self, spec, result):
                return Validation(verdict='done')

        self.orch.backend = FakeBackend()
        self.orch._runs['s1'] = ShotRun(shot_id='s1', status='queued')
        with patch.object(self.orch, '_relay_speech') as relay:
            self.orch._run_one(self.slug, 's1')
        relay.assert_not_called()
        updated = self.orch.store.load(self.slug)
        self.assertEqual(updated['shots'][0]['renderedDialogueSource'], 'native')
        self.assertIsNone(updated['shots'][0]['dubUrl'])

    def test_fingerprint_layering_version_bump_invalidates_old_renders(self):
        # A forgotten layeringVersion/ref2vaProfile bump would ship this whole
        # change without marking a single already-rendered clip stale. Prove
        # the bump actually happened by running the OLD render_fingerprint
        # (from the last commit, before this change) against the same inputs
        # and checking it no longer matches.
        import re
        import subprocess
        repo_root = Path(__file__).resolve().parent.parent
        old_source = subprocess.run(
            ['git', 'show', 'HEAD:server/store.py'],
            cwd=str(repo_root), capture_output=True, text=True, check=True,
        ).stdout
        match = re.search(r'\ndef render_fingerprint\(.*?\n(?=\ndef )', old_source, re.S)
        self.assertIsNotNone(match, "could not find render_fingerprint in HEAD's store.py")
        from server.store import _ref_key, speech_fingerprint
        import hashlib, json
        ns = {'_ref_key': _ref_key, 'speech_fingerprint': speech_fingerprint,
              'hashlib': hashlib, 'json': json, 'Any': object, 'dict': dict}
        exec(compile(match.group(0), '<old render_fingerprint>', 'exec'), ns)
        old_render_fingerprint = ns['render_fingerprint']

        shot = {**self.board['shots'][0], 'startRef': {'path': 'anchor.png'}}
        old_hash = old_render_fingerprint(shot, self.board)
        new_hash = render_fingerprint(shot, self.board)
        self.assertNotEqual(old_hash, new_hash)

    @unittest.skipUnless(shutil.which('ffmpeg'),'ffmpeg required')
    def test_trim_crossfade_silent_clip_and_continuous_bed(self):
        one,two=self.root/'one.mp4',self.root/'two.mp4'
        make_clip(one,2,with_audio=True);make_clip(two,2,colour='blue',with_audio=False)
        settings=dict(transitionSeconds=.25,audioFadeSeconds=.05,normalizeAudio=True,
                      backgroundPath=str(one),backgroundVolume=.1,
                      trims={str(one):[.25,.25]})
        out=self.root/'final.mp4'
        result=assemble.assemble([('one',one),('two',two)],out,320,176,options=settings)
        self.assertTrue(result.ok,(result.error,result.log))
        self.assertAlmostEqual(result.seconds,3.25,delta=.12)
        self.assertTrue(assemble._has_audio(out))
        prior=out.read_bytes()
        settings['trims'][str(one)]=[3,0]
        bad=assemble.assemble([('one',one),('two',two)],out,320,176,options=settings)
        self.assertFalse(bad.ok)
        self.assertEqual(prior,out.read_bytes())

    def test_legacy_voice_fingerprint_upgrades_without_resynthesis(self):
        board=self.board
        voice=self.root/'test/refs/voice.wav'
        voice.parent.mkdir(parents=True,exist_ok=True);voice.write_bytes(b'original reference')
        board['characters']=[dict(id='c',voice={'path':str(voice.relative_to(self.root))})]
        shot=board['shots'][0]
        shot.update(characterIds=['c'],dialogue='Hello',dialogueSource='recording')
        self.orch.store.migrate(board)
        shot['speechFingerprint']=speech_fingerprint(shot,board)
        self.orch.store.save(self.slug,board)
        loaded=self.orch.store.load(self.slug)
        self.assertEqual(loaded['shots'][0]['speechFingerprint'],speech_fingerprint(loaded['shots'][0],loaded))
        voice.write_bytes(b'a changed reference at the same filename')
        changed=self.orch.store.load(self.slug)
        self.assertNotEqual(changed['shots'][0]['speechFingerprint'],speech_fingerprint(changed['shots'][0],changed))

    def test_cut_settings_do_not_invalidate_video(self):
        shot=self.board['shots'][0]
        old=render_fingerprint(shot,self.board)
        cut=assemble.assembly_fingerprint(self.board)
        shot['trimIn']=.25
        self.board['assembly']={'transitionSeconds':.2}
        self.assertEqual(old,render_fingerprint(shot,self.board))
        self.assertNotEqual(cut,assemble.assembly_fingerprint(self.board))

if __name__ == '__main__': unittest.main()
