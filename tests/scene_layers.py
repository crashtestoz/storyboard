"""Render input contracts: python3 tests/scene_layers.py (no model required)."""
import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server.backends.vpipe_backend import (
    ASPECT_TABLE, H3_BASE_RESOLUTIONS, _clones_voice,
    _effective_video_model, _reference_bindings,
    _ref2va_references, _resolved_prompt,
)
from server.backends.base import FrameRule, ModelCapability, ShotPaths
from server.store import default_shot, render_fingerprint
from server.llm import _parse_character_result

class SceneLayers(unittest.TestCase):
    def setUp(self):
        self.board = dict(sceneDescription='Shared corridor.', soundscape='Ventilation.', characters=[
            dict(id='c1', name='Droid', description='Gold plating.', image={'path':'gold.png'}, voice={'path':'voice.wav'})], defaults={})
        self.shot = dict(id='s1', model='ref2va', prompt='Walk through @corridor.', characterIds=['c1'],
                         referenceImages=[dict(path='room.png',tag='corridor',role='environment')],
                         dialogue='Yes, sir.', dialogueStyle='Quietly.', dialogueSource='native', soundNote='Metal footsteps.')
    def test_layers_and_mapping(self):
        prompt = _resolved_prompt(self.shot,self.board,model='ref2va')
        for part in ['Shared corridor.', 'Gold plating.', 'Walk through <Picture 1>.', 'Ventilation.', 'Metal footsteps.', 'Quietly.']:
            self.assertIn(part,prompt)
        refs = _ref2va_references(self.shot,self.board,ShotPaths(Path('/tmp'),Path('/tmp/shot'),'shot',Path('/tmp')))
        self.assertEqual([Path(r).name for r in refs], ['room.png','gold.png','voice.wav'])
        self.shot['referenceImages'].insert(0,dict(path='other.png'))
        self.assertIn('Walk through <Picture 2>.',_resolved_prompt(self.shot,self.board,model='ref2va'))
    def test_recording_excludes_voice_reference(self):
        self.shot['dialogueSource']='recording'
        self.assertFalse(_clones_voice(self.shot,self.board,'ref2va'))
        p=_resolved_prompt(self.shot,self.board,model='ref2va')
        self.assertIn('dubbed separately',p)
        refs=_ref2va_references(self.shot,self.board,ShotPaths(Path('/tmp'),Path('/tmp/shot'),'shot',Path('/tmp')))
        self.assertNotIn('/tmp/voice.wav',refs)
        self.assertEqual(default_shot()['dialogueSource'],'recording')

    def test_native_dialogue_overrides_quiet_soundscape(self):
        prompt = _resolved_prompt(self.shot, self.board, model='ref2va')
        self.assertIn('speaks aloud, in their own voice', prompt)
        self.assertIn('Dialogue priority:', prompt)
    def test_unknown_and_duplicate_tags(self):
        self.shot['prompt']='Look at @missing.'
        with self.assertRaises(ValueError): _resolved_prompt(self.shot,self.board,model='ref2va')
        self.shot['referenceImages'].append(dict(path='second.png',tag='corridor'))
        with self.assertRaises(ValueError): _reference_bindings(self.shot,self.board,'ref2va')
    def test_reference_limit_is_explicit(self):
        self.shot['referenceImages']=[dict(path=f'{i}.png') for i in range(9)]
        with self.assertRaises(ValueError): _reference_bindings(self.shot,self.board,'ref2va')
    def test_changed_render_inputs(self):
        original=render_fingerprint(self.shot,self.board)
        for field,value in [('dialogueStyle','Loud'),('dialogueSource','recording'),('speakerId','c2')]:
            shot={**self.shot,field:value}
            self.assertNotEqual(original,render_fingerprint(shot,self.board))
        board=copy.deepcopy(self.board);board['characters'][0]['voice']['path']='new.wav'
        self.assertNotEqual(original,render_fingerprint(self.shot,board))
        shot=copy.deepcopy(self.shot);shot['referenceImages'][0]['role']='style'
        self.assertNotEqual(original,render_fingerprint(shot,self.board))
    def test_unselected_character_not_added(self):
        self.shot['characterIds']=[]
        self.assertNotIn('Gold plating.',_resolved_prompt(self.shot,self.board,model='ref2va'))

    def test_frame_anchors_select_fl2va(self):
        shot = {**self.shot, 'model': 'ref2va'}
        self.assertEqual(_effective_video_model(shot, self.board)[0], 'ref2va')
        shot['startRef'] = {'path': 'opening.png'}
        self.assertEqual(_effective_video_model(shot, self.board)[0], 'fl2va')
        self.assertIn('Picture 1 is the Start frame at 0.00 seconds',
                      _resolved_prompt(shot, self.board, model='fl2va'))
        shot.pop('startRef')
        shot['endRef'] = {'path': 'closing.png'}
        self.assertEqual(_effective_video_model(shot, self.board)[0], 'fl2va')
        self.assertIn('Picture 1 is the End frame',
                      _resolved_prompt(shot, self.board, model='fl2va'))
        shot['startRef'] = {'path': 'opening.png'}
        self.assertEqual(_effective_video_model(shot, self.board)[0], 'fl2va')
        self.assertEqual(_reference_bindings(shot, self.board, 'fl2va'), [])

    def test_project_style_refs_are_a_library_not_auto_combined(self):
        self.board['styleRefs'] = [{'path': 'style.png'}]
        shot = {**self.shot, 'startRef': {'path': 'opening.png'},
                'endRef': {'path': 'closing.png'}}
        refs = _ref2va_references(
            shot, self.board,
            ShotPaths(Path('/tmp'), Path('/tmp/shot'), 'shot', Path('/tmp')),
        )
        self.assertEqual([Path(r).name for r in refs],
                         ['opening.png', 'closing.png', 'room.png', 'gold.png',
                          'voice.wav'])

    def test_h3_base_frame_sizes(self):
        self.assertEqual(list(ASPECT_TABLE), ['21:9', '16:9', '4:3', '1:1', '3:4', '9:16'])
        self.assertEqual(H3_BASE_RESOLUTIONS,
                         ['1792x768', '1344x768', '1024x768',
                          '768x768', '768x1024', '768x1344'])
        for sizes in ASPECT_TABLE.values():
            for size in sizes:
                width, height = map(int, size.split('x'))
                self.assertEqual(width % 32, 0)
                self.assertEqual(height % 32, 0)

    def test_backend_preview_uses_same_prompt(self):
        from server.app import Handler
        from types import SimpleNamespace
        payload = dict(board=self.board, shot=self.shot)
        fake = SimpleNamespace(ctx=SimpleNamespace(backend=SimpleNamespace(capability=lambda model: SimpleNamespace(supports_audio=True))),
                               _read_json=lambda: payload, _send_json=lambda data: data)
        result = Handler._api_post(fake, '/api/render-preview')
        self.assertEqual(result['prompt'], _resolved_prompt(self.shot,self.board,model='ref2va'))
        self.assertEqual(result['dialogueSource'], 'H3 native speech')
        self.shot['dialogueSource']='recording'
        self.assertEqual(Handler._api_post(fake, '/api/render-preview')['dialogueSource'], 'Dialogue-window recording')

    def test_create_stills_ignores_dialogue_validation(self):
        from server.backends.vpipe_backend import VpipeBackend
        from tempfile import TemporaryDirectory

        backend = VpipeBackend(Path('/tmp/vpipe'), Path('/tmp/workspace'))
        backend.capability = lambda model: ModelCapability(
            id='krea2-still', label='Krea test', kind='image',
            supports_audio=False, frame_rule=FrameRule(),
            resolutions=['480x480'], available=True,
        )
        with TemporaryDirectory() as temp:
            paths = ShotPaths(Path('/tmp/workspace'), Path(temp), 'test', Path('/tmp'))
            shot = {'id': 'still-only', 'model': 'krea2-still',
                    'prompt': 'A person looks toward the camera.',
                    'dialogueSource': 'recording',
                    'dialogue': 'This is only for the video.'}
            spec = backend.prepare(shot, {'defaults': {}}, paths)
            self.assertEqual(spec.payload['model'], 'krea2-still')

    def test_character_result_is_split_into_character_and_environment(self):
        result = _parse_character_result(
            'CHARACTER:\nA person with dark curly hair and a red jacket.\n\n'
            'ENVIRONMENT:\nA bright kitchen with wooden cabinets.'
        )
        self.assertEqual(result['character'], 'A person with dark curly hair and a red jacket.')
        self.assertEqual(result['environment'], 'A bright kitchen with wooden cabinets.')

    def test_character_result_accepts_json(self):
        result = _parse_character_result(
            '{"character": "A silver helmet.", "environment": "A hangar."}'
        )
        self.assertEqual(result, {'character': 'A silver helmet.', 'environment': 'A hangar.'})

    def test_character_result_keeps_legacy_plain_text_as_character_only(self):
        result = _parse_character_result('A person in a blue coat.')
        self.assertEqual(result, {'character': 'A person in a blue coat.', 'environment': ''})

if __name__=='__main__': unittest.main()
