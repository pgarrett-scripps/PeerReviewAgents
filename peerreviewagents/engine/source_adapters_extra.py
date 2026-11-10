"""Bounded exact-record adapters for additional public scientific sources."""
import re
from urllib.parse import quote, urlencode


PROVIDERS = {
    'string-interactions': ('Exact taxon and protein pair',
        'STRING reports functional associations. Scores and evidence channels do not prove direct binding or causal regulation.'),
    'ensembl': ('Stable gene or transcript ID',
        'Gene models, coordinates, and transcript designations are assembly and release specific. Annotation does not establish function.'),
    'reactome': ('Stable pathway or reaction ID',
        'A curated pathway record does not establish activity in the manuscript tissue, condition, or experiment.'),
    'gbif-occurrence': ('Numeric occurrence key',
        'An occurrence record reflects submitted evidence and does not establish abundance, standardized sampling, or geographic absence.'),
    'nasa-cmr': ('Collection concept ID',
        'Collection metadata describes a data product. It does not validate granule contents, processing suitability, or a manuscript result.'),
    'worldbank': ('Indicator code',
        'Indicator metadata defines a series. It does not establish that a manuscript selected the correct country, year, revision, or transformation.'),
}


def request(provider, identifier):
    if provider not in PROVIDERS:
        return None
    if not isinstance(identifier, str) or identifier != identifier.strip() or len(identifier) > 160:
        raise ValueError('Use a short exact source identifier')
    if provider == 'string-interactions':
        match = re.fullmatch(r'(\d{1,9}):([A-Za-z0-9_.-]{1,60}),([A-Za-z0-9_.-]{1,60})', identifier)
        if not match or match.group(2).casefold() == match.group(3).casefold():
            raise ValueError('STRING interaction ID must be taxon:proteinA,proteinB')
        taxon, protein_a, protein_b = match.groups()
        return 'https://string-db.org/api/json/network?' + urlencode({
            'identifiers': protein_a + '\r' + protein_b, 'species': taxon,
            'required_score': 0, 'network_type': 'functional',
        })
    patterns = {
        'ensembl': r'(?:ENS[A-Z]{0,6}[GT]\d{6,15})(?:\.\d+)?',
        'reactome': r'R-[A-Z]{3}-\d+(?:\.\d+)?',
        'gbif-occurrence': r'\d{1,18}',
        'nasa-cmr': r'C\d{5,18}-[A-Za-z0-9_-]{1,40}',
        'worldbank': r'[A-Za-z0-9][A-Za-z0-9.]{1,39}',
    }
    if not re.fullmatch(patterns[provider], identifier):
        raise ValueError('Identifier format does not match ' + provider)
    encoded = quote(identifier, safe='')
    if provider == 'ensembl':
        stable_id = identifier.split('.', 1)[0]
        return 'https://rest.ensembl.org/lookup/id/' + quote(stable_id, safe='') + '?' + urlencode({'content-type':'application/json', 'expand':0})
    if provider == 'reactome':
        return 'https://reactome.org/ContentService/data/query/' + encoded
    if provider == 'gbif-occurrence':
        return 'https://api.gbif.org/v1/occurrence/' + encoded
    if provider == 'nasa-cmr':
        return 'https://cmr.earthdata.nasa.gov/search/collections.json?' + urlencode({'concept_id':identifier, 'page_size':2})
    return 'https://api.worldbank.org/v2/indicator/' + encoded + '?' + urlencode({'format':'json', 'per_page':2})


def selected(data, keys):
    return {key:data[key] for key in keys if key in data}


def parse(provider, identifier, data):
    """Return selected fields, record version, publication date, and exact identity."""
    version = None
    published = None
    resolved = None
    if provider == 'string-interactions':
        if not isinstance(data, list):
            raise TypeError('STRING response must be a list')
        taxon, protein_a, protein_b = re.fullmatch(
            r'(\d{1,9}):([A-Za-z0-9_.-]{1,60}),([A-Za-z0-9_.-]{1,60})', identifier).groups()
        expected = {protein_a.casefold(), protein_b.casefold()}
        matches = [row for row in data if str(row.get('ncbiTaxonId')) == taxon and
                   {str(row.get('preferredName_A', '')).casefold(),
                    str(row.get('preferredName_B', '')).casefold()} == expected]
        if len(matches) != 1:
            return {'candidate_count':len(matches), 'response_edges':len(data),
                    'selection_limit':'No interaction fields returned unless exactly one edge matches the requested taxon and unordered protein pair.'}, None, None, None
        row = matches[0]
        fields = selected(row, ('stringId_A','stringId_B','preferredName_A','preferredName_B',
            'ncbiTaxonId','score','nscore','fscore','pscore','ascore','escore','dscore','tscore'))
        fields['association_channels'] = {
            'neighborhood':row.get('nscore'), 'fusion':row.get('fscore'),
            'phylogenetic_cooccurrence':row.get('pscore'), 'coexpression':row.get('ascore'),
            'experimental':row.get('escore'), 'database':row.get('dscore'),
            'text_mining':row.get('tscore'),
        }
        fields['selection_limit'] = 'One exact pair only. Scores are associations and do not establish direct binding.'
        resolved = identifier
    elif provider == 'ensembl':
        fields = selected(data, ('id','object_type','species','assembly_name','biotype','source',
            'seq_region_name','start','end','strand','version','canonical_transcript','display_name','description'))
        response_id = str(data.get('id', ''))
        requested_base = identifier.split('.')[0]
        if response_id != requested_base:
            return fields, str(data.get('version')) if data.get('version') is not None else None, None, None
        version = str(data.get('version')) if data.get('version') is not None else None
        if '.' in identifier and version != identifier.rsplit('.', 1)[1]:
            return fields, version, None, None
        fields['selection_limit'] = 'One stable gene or transcript record. Child features and sequence are omitted.'
        resolved = identifier
    elif provider == 'reactome':
        stable = data.get('stId')
        stable_record = data.get('stableIdentifier') or {}
        if stable is None:
            stable = stable_record.get('identifier')
        fields = selected(data, ('dbId','stId','displayName','schemaClass','isInDisease','isInferred'))
        species = data.get('species')
        fields['species'] = [selected(row, ('dbId','displayName','name')) for row in species[:4]] if isinstance(species, list) else species
        fields['summation'] = [selected(row, ('text',)) for row in data.get('summation', [])[:2]]
        fields['selection_limit'] = 'At most four species and two summations. Participants, hierarchy, and diagrams are omitted.'
        version_value = stable_record.get('identifierVersion')
        version = str(version_value) if version_value is not None else None
        if stable != identifier and not (version and identifier == stable + '.' + version):
            return fields, version, None, None
        resolved = identifier
    elif provider == 'gbif-occurrence':
        fields = selected(data, ('key','scientificName','acceptedScientificName','taxonKey',
            'acceptedTaxonKey','occurrenceStatus','basisOfRecord','eventDate','year','countryCode',
            'decimalLatitude','decimalLongitude','coordinateUncertaintyInMeters','datasetKey',
            'institutionCode','collectionCode','catalogNumber','recordedBy','identifiedBy','modified'))
        issues = data.get('issues', [])
        fields['issues'] = issues[:12] if isinstance(issues, list) else []
        fields['issues_total'] = len(issues) if isinstance(issues, list) else 0
        fields['selection_limit'] = 'First 12 quality flags. Multimedia, verbatim fields, and extensions are omitted.'
        resolved = str(data.get('key')) if data.get('key') is not None else None
        version = data.get('modified')
    elif provider == 'nasa-cmr':
        entries = data.get('feed', {}).get('entry', [])
        matches = [row for row in entries if row.get('id') == identifier]
        if len(matches) != 1:
            return {'candidate_count':len(matches), 'response_records':len(entries),
                    'selection_limit':'No collection fields returned unless exactly one concept ID matches.'}, None, None, None
        row = matches[0]
        fields = selected(row, ('id','title','dataset_id','short_name','version_id','updated',
            'time_start','time_end','data_center','organizations'))
        fields['links'] = [selected(link, ('href','rel','title','type')) for link in row.get('links', [])[:8]]
        fields['selection_limit'] = 'First 8 links. Granules, spatial polygons, science keywords, and full summaries are omitted.'
        resolved = row.get('id')
        version = row.get('version_id') or row.get('updated')
        published = (row.get('insert_time') or '')[:10] or None
    elif provider == 'worldbank':
        if not isinstance(data, list) or len(data) < 2 or not isinstance(data[1], list):
            raise TypeError('World Bank response must contain metadata and records')
        matches = [row for row in data[1] if row.get('id') == identifier]
        if len(matches) != 1:
            return {'candidate_count':len(matches), 'response_records':len(data[1]),
                    'selection_limit':'No indicator fields returned unless exactly one indicator code matches.'}, None, None, None
        row = matches[0]
        fields = selected(row, ('id','name','unit','sourceNote','sourceOrganization'))
        source = row.get('source')
        fields['source'] = selected(source, ('id','value')) if isinstance(source, dict) else source
        topics = row.get('topics', [])
        fields['topics'] = [selected(topic, ('id','value')) for topic in topics[:8]]
        fields['selection_limit'] = 'Indicator metadata only, with at most 8 topics. Country-year values are not retrieved.'
        resolved = row.get('id')
        version = data[0].get('lastupdated') if isinstance(data[0], dict) else None
    else:
        raise ValueError('Unknown source parser')
    return fields, version, published, resolved
