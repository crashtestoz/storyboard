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
from server.backends.base import ShotPaths
from server.store import default_shot, render_fingerprint

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

    def test_model_partition_follows_incompatible_inputs(self):
        shot = {**self.shot, 'model': 'fl2va'}
        self.assertEqual(_effective_video_model(shot, self.board)[0], 'ref2va')
        shot['startRef'] = {'path': 'opening.png'}
        self.assertEqual(_effective_video_model(shot, self.board)[0], 'fl2va')
        self.assertEqual(_reference_bindings(shot, self.board, 'fl2va'), [])
        shot.pop('startRef')
        shot['model'] = 'ref2va'
        shot['endRef'] = {'path': 'closing.png'}
        self.assertEqual(_effective_video_model(shot, self.board)[0], 'fl2va')

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

if __name__=='__main__': unittest.main()
