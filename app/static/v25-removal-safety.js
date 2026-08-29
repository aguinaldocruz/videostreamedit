function removedStreamSignatures(before, after) {
  const beforeById = new Map(before.rows.map(row => [row.id, row]));
  return after.rows.filter(row => row.removed).map(row => {
    const original = beforeById.get(row.id);
    return {...original, default: before.defaultAudio === row.id || before.defaultSubtitle === row.id, forced: before.forcedAudio === row.id || before.forcedSubtitle === row.id};
  });
}

function removalTemplateMatches(template, current) {
  const currentById = new Map(current.rows.map(row => [row.id, row]));
  return removedStreamSignatures(template.before, template.after).every(signature => {
    const row = currentById.get(signature.id);
    if (!row) return false;
    const candidate = {...row, default: current.defaultAudio === row.id || current.defaultSubtitle === row.id, forced: current.forcedAudio === row.id || current.forcedSubtitle === row.id};
    return JSON.stringify(candidate) === JSON.stringify(signature);
  });
}
