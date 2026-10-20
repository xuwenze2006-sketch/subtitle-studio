"""Small real-format approved subtitle artifacts for Studio delivery tests."""
import json
from pathlib import Path

from subtitle_pipeline.integrity import fingerprint, sha256
from subtitle_pipeline.languages import manifest_languages, output_names


def approve_export_fixture(campaign, manifest):
    """Bind existing or fresh owned machine SRTs; never bypass delivery checks."""
    campaign = Path(campaign)
    language, target = manifest_languages(manifest)
    folder = campaign / '整片'
    folder.mkdir(exist_ok=True)
    for name, text in zip(output_names(target), ('source', 'translation', 'translation\nsource')):
        path = folder / name
        if not path.exists():
            path.write_text('1\n00:00:00,000 --> 00:00:01,000\n' + text + '\n', encoding='utf-8')
    if not (folder / 'state.json').exists():
        (folder / 'state.json').write_text('{"status":"complete"}', encoding='utf-8')
    issues = folder / '需复核.json'
    if not issues.exists():
        issues.write_text('[]', encoding='utf-8')
    artifacts = [{'path': str((folder / name).resolve()), 'sha256': sha256(folder / name)}
                 for name in (*output_names(target), '需复核.json')]
    review = {'status': 'sampled_approved', 'source_sha256': manifest['source']['sha256'],
              'language': language, 'target': target, 'artifacts': artifacts,
              'synthetic_fixture': True, 'human_reviewed': False}
    (campaign / 'final-review.json').write_text(json.dumps(review, ensure_ascii=False), encoding='utf-8')
    manifest['output_binding'].update(captions_sha256=sha256(folder / output_names(target)[1]),
                                      review_artifacts_sha256=fingerprint(artifacts))
    return review
