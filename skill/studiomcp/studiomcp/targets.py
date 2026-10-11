"""One Studio window = one target; Edit / Play-server / Play-client are contexts of it.

The plugin registers a separate receiver for every DataModel it runs in, so a Studio in Play
shows up as three receivers. Callers think in Studios, so receivers that share a place and
user are folded into one target with named contexts.
"""


def context_name(receiver):
    if receiver.get('datamodel', 'Edit') != 'Play':
        return 'Edit'
    return 'Play/' + receiver.get('context', 'client')


def group(receivers):
    """Targets in first-seen receiver order, each {index, name, placeId, userId, mode, contexts, receiver}.

    `receiver` is the id routing starts from (the Edit receiver when there is one);
    `Environment.request` then re-routes to the right context for each command.
    """
    targets = {}
    for receiver in receivers:
        key = (receiver.get('placeId'), receiver.get('userId'))
        target = targets.setdefault(key, {'name': receiver.get('name'), 'placeId': key[0], 'userId': key[1], 'contexts': {}})
        target['contexts'][context_name(receiver)] = receiver['studio']
    ordered = []
    for index, target in enumerate(targets.values(), 1):
        contexts = target['contexts']
        ordered.append({**target, 'index': index, 'mode': 'Play' if any(name.startswith('Play') for name in contexts) else 'Edit',
                        'contexts': sorted(contexts), 'receiver': contexts.get('Edit') or next(iter(contexts.values()))})
    return ordered
