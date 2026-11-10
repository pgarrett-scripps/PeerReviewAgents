"""Small exact-record queries. Source semantics stay visible to reviewers."""
import re
from urllib.parse import quote, urlencode
from . import source_adapters_extra as extra


PROVIDERS = {
    'crossref': ('DOI metadata', 'Bibliographic metadata does not verify citation support.'),
    'datacite': ('DOI metadata', 'Depositor metadata does not validate dataset contents.'),
    'europepmc': ('PMID abstract', 'Abstract evidence does not establish full-text support.'),
    'uniprot': ('Protein accession', 'Annotation and sequence do not establish the manuscript mechanism. Match organism and isoform.'),
    'rcsb': ('PDB entry ID', 'A structure record does not establish cellular mechanism or direct binding in the manuscript conditions.'),
    'pubchem': ('Numeric compound CID', 'Match stereochemistry, salt and assay context before comparing compounds.'),
    'chembl': ('CHEMBL assay ID', 'Assays and endpoints are context specific. Activity is not interchangeable across assays.'),
    'clinicaltrials': ('NCT study ID', 'This is the current sponsor-supplied record, not the original registered plan. Historical comparison requires record history.'),
    'osf': ('Public registration ID', 'A public registration is not proof of adherence. Private or embargoed records may be inaccessible.'),
    'gbif': ('Numeric taxon key', 'Taxonomic records do not establish geographic absence or standardized population sampling.'),
    'string': ('Taxon:protein name, for example 9606:TP53', 'Identity resolution only. This endpoint does not establish an interaction. STRING associations do not prove direct binding.'),
    'pride': ('PXD project accession', 'Deposition metadata does not validate spectra, localization, quantification or study conclusions.'),
}
PROVIDERS.update(extra.PROVIDERS)


def request(provider, identifier):
    if provider in extra.PROVIDERS:
        return extra.request(provider, identifier)
    if provider not in PROVIDERS:
        return None
    if not isinstance(identifier, str) or len(identifier) > 200 or identifier != identifier.strip():
        raise ValueError('Use a short exact source identifier')
    patterns = {'crossref': r'10\.\d{4,9}/\S+', 'datacite': r'10\.\d{4,9}/\S+',
                'europepmc': r'\d{1,12}', 'uniprot': r'[A-Z0-9]{6,10}(?:-\d+)?',
                'rcsb': r'[0-9][A-Za-z0-9]{3}', 'pubchem': r'\d{1,12}',
                'chembl': r'CHEMBL\d+', 'clinicaltrials': r'NCT\d{8}',
                'osf': r'[a-z0-9]{5,12}', 'gbif': r'\d{1,12}',
                'string': r'\d{1,9}:[A-Za-z0-9_.-]{1,60}', 'pride': r'PXD\d{6,9}'}
    if not re.fullmatch(patterns[provider], identifier):
        raise ValueError('Identifier format does not match ' + provider)
    encoded = quote(identifier, safe='')
    if provider == 'string':
        taxon, symbol = identifier.split(':')
        return 'https://string-db.org/api/json/get_string_ids?' + urlencode({'identifiers': symbol, 'species': taxon, 'limit': 2, 'echo_query': 1})
    return {
        'crossref': 'https://api.crossref.org/works/' + encoded,
        'datacite': 'https://api.datacite.org/dois/' + encoded,
        'europepmc': 'https://www.ebi.ac.uk/europepmc/webservices/rest/search?' + urlencode({'query': 'EXT_ID:' + identifier + ' AND SRC:MED', 'format': 'json', 'resultType': 'core', 'pageSize': 1}),
        'uniprot': 'https://rest.uniprot.org/uniprotkb/' + encoded + '.json',
        'rcsb': 'https://data.rcsb.org/rest/v1/core/entry/' + encoded.upper(),
        'pubchem': 'https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/' + encoded + '/property/MolecularFormula,MolecularWeight,IUPACName,InChIKey/JSON',
        'chembl': 'https://www.ebi.ac.uk/chembl/api/data/assay/' + encoded + '.json',
        'clinicaltrials': 'https://clinicaltrials.gov/api/v2/studies/' + encoded,
        'osf': 'https://api.osf.io/v2/registrations/' + encoded + '/',
        'gbif': 'https://api.gbif.org/v1/species/' + encoded,
        'pride': 'https://www.ebi.ac.uk/pride/ws/archive/v2/projects/' + encoded,
    }[provider]


def selected(data, keys):
    return {key: data[key] for key in keys if key in data}


def parse(provider, identifier, data):
    """Return fields, version, publication date and resolved identity."""
    if provider in extra.PROVIDERS:
        return extra.parse(provider, identifier, data)
    version = None
    published = None
    if provider == 'crossref':
        row = data['message']
        fields = selected(row, ('DOI', 'title', 'author', 'published', 'type', 'relation', 'update-to', 'updated-by'))
        parts = row.get('published', {}).get('date-parts', [[]])[0]
        published = '-'.join(str(n).zfill(2) for n in (parts + [1, 1])[:3]) if parts else None
        resolved = row.get('DOI')
    elif provider == 'datacite':
        row = data['data']['attributes']
        fields = selected(row, ('doi', 'titles', 'creators', 'publicationYear', 'types', 'relatedIdentifiers', 'url', 'version'))
        version, resolved = row.get('version'), row.get('doi')
    elif provider == 'europepmc':
        rows = data['resultList']['result']
        if not rows:
            return {}, None, None, None
        row = rows[0]
        fields = selected(row, ('id', 'source', 'doi', 'title', 'authorString', 'firstPublicationDate', 'abstractText', 'isOpenAccess'))
        resolved, published = row.get('id'), row.get('firstPublicationDate')
    elif provider == 'uniprot':
        fields = selected(data, ('primaryAccession', 'entryType', 'proteinDescription', 'genes', 'organism', 'uniProtKBCrossReferences', 'entryAudit'))
        fields.pop('uniProtKBCrossReferences', None)
        fields['sequence'] = selected(data.get('sequence', {}), ('length', 'molWeight', 'crc64'))
        features = data.get('features', [])
        fields['features'] = features[:12]
        fields['features_total'] = len(features)
        fields['feature_selection_limit'] = 'First 12 annotations only. This is not an exhaustive site or PTM check.'
        resolved = data.get('primaryAccession')
        version = str(data.get('entryAudit', {}).get('entryVersion', '')) or None
    elif provider == 'rcsb':
        fields = selected(data, ('rcsb_id', 'struct', 'exptl', 'rcsb_entry_info', 'rcsb_accession_info', 'rcsb_entry_container_identifiers'))
        resolved = data.get('rcsb_id')
        version = data.get('rcsb_accession_info', {}).get('revision_date')
    elif provider == 'pubchem':
        row = data['PropertyTable']['Properties'][0]
        fields, resolved = row, str(row.get('CID'))
    elif provider == 'chembl':
        fields = selected(data, ('assay_chembl_id', 'description', 'assay_type', 'assay_organism', 'assay_tax_id', 'target_chembl_id', 'confidence_score', 'document_chembl_id', 'bao_format'))
        resolved = data.get('assay_chembl_id')
    elif provider == 'clinicaltrials':
        row = data['protocolSection']
        fields = selected(row, ('identificationModule', 'statusModule', 'designModule'))
        arms = row.get('armsInterventionsModule', {})
        fields['arms'] = [selected(a, ('label', 'type')) for a in arms.get('armGroups', [])[:8]]
        fields['interventions'] = [selected(a, ('type', 'name', 'armGroupLabels')) for a in arms.get('interventions', [])[:8]]
        outcomes = row.get('outcomesModule', {})
        fields['outcomes'] = {k: [selected(v, ('measure', 'timeFrame')) for v in outcomes.get(k, [])[:3]]
                              for k in ('primaryOutcomes', 'secondaryOutcomes')}
        fields['outcome_counts'] = {k: len(outcomes.get(k, [])) for k in ('primaryOutcomes', 'secondaryOutcomes')}
        fields['eligibility'] = selected(row.get('eligibilityModule', {}), ('sex', 'minimumAge', 'maximumAge', 'healthyVolunteers'))
        fields['selection_limits'] = 'First 8 arms and interventions, first 3 outcomes per type. Detailed descriptions and eligibility criteria are omitted. Omission from this card is not absence from the record.'
        resolved = row.get('identificationModule', {}).get('nctId')
        version = row.get('statusModule', {}).get('lastUpdatePostDateStruct', {}).get('date')
        fields['historical_state'] = 'unknown'
    elif provider == 'osf':
        row = data['data']
        fields = selected(row['attributes'], ('title', 'description', 'date_created', 'date_registered', 'registration_responses', 'registered_meta', 'withdrawn', 'embargoed'))
        resolved = row.get('id')
        version = fields.get('date_registered')
        fields['historical_state'] = 'Retrieved registration content. Amendment and attachment history not checked.'
    elif provider == 'gbif':
        fields = selected(data, ('key', 'scientificName', 'canonicalName', 'rank', 'taxonomicStatus', 'acceptedKey', 'accepted', 'kingdom', 'phylum', 'class', 'order', 'family', 'genus', 'species', 'sourceTaxonKey'))
        resolved = str(data.get('key'))
    elif provider == 'string':
        if len(data) != 1:
            return {'candidates': data[:2]}, None, None, None
        fields = selected(data[0], ('queryItem', 'stringId', 'ncbiTaxonId', 'taxonName', 'preferredName', 'annotation'))
        taxon, symbol = identifier.split(':')
        if str(fields.get('ncbiTaxonId')) != taxon or str(fields.get('preferredName', '')).casefold() != symbol.casefold():
            return fields, None, None, None
        resolved = identifier
    elif provider == 'pride':
        fields = selected(data, ('accession', 'title', 'projectDescription', 'sampleProcessingProtocol', 'dataProcessingProtocol', 'submissionType', 'publicationDate', 'organisms', 'instruments', 'references'))
        resolved = data.get('accession')
    else:
        raise ValueError('Unknown source parser')
    return fields, version, published, resolved
