"""Exact source references for blinded model assessments."""
import copy
from .evidence_handles import hydrate_response


def judge_schema(schema, registry, images):
    result = copy.deepcopy(schema)
    ids = [h.handle_id for h in registry.handles]
    choice = ({'type': 'string', 'enum': ids} if len(ids) <= 950 else
              {'type': 'string', 'pattern': '^(?:' + '|'.join(ids) + ')$'})
    result.setdefault('$defs', {})['judgeSourceHandle'] = choice
    reference = {'anyOf': [
        {'type': 'object', 'properties': {
            'evidence_handle': {'$ref': '#/$defs/judgeSourceHandle'}},
         'required': ['evidence_handle'], 'additionalProperties': False}]}
    if images:
        reference['anyOf'].append({'type': 'object', 'properties': {
            'image_id': {'type': 'string', 'enum': [i['id'] for i in images]},
            'visual_observation': {'type': 'string', 'minLength': 1}},
            'required': ['image_id', 'visual_observation'], 'additionalProperties': False})
    rating = result['properties']['conditions']['items']['properties']['ratings']['items']
    for name in ('evidence', 'counterevidence'):
        rating['properties'][name]['items'] = copy.deepcopy(reference)
    return result


def hydrate_judgment(value, registry, images):
    """Expand text handles and preserve separately validated image observations."""
    result = copy.deepcopy(value)
    allowed_images = {i['id'] for i in images}
    receipts = []
    for condition in result['conditions']:
        for rating in condition['ratings']:
            for field in ('evidence', 'counterevidence'):
                refs = []
                for reference in rating[field]:
                    if set(reference) == {'evidence_handle'}:
                        hydrated = hydrate_response({'evidence': [reference]}, registry)
                        refs.append({**hydrated['response']['evidence'][0],
                                     'image_id': '', 'visual_observation': ''})
                        receipts.append({'finding_id': rating['finding_id'], 'field': field,
                                         'audit': hydrated['audit']})
                    elif (set(reference) == {'image_id', 'visual_observation'}
                          and reference['image_id'] in allowed_images
                          and isinstance(reference['visual_observation'], str)
                          and reference['visual_observation'].strip()):
                        refs.append({**reference, 'block_id': '', 'quote': ''})
                    else:
                        raise ValueError('Invalid judge source reference')
                rating[field] = refs
    return {'response': result, 'receipts': receipts,
            'registry_manifest_sha256': registry.manifest_sha256}
