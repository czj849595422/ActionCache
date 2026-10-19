'use strict'

const minecraftData = require('minecraft-data')
const prismarineRecipe = require('prismarine-recipe')

function namespacedItemName (registry, itemId) {
  const item = registry.items[itemId]
  if (!item || !item.name) {
    throw new Error(`minecraft-data item id ${itemId} is missing`)
  }
  return `minecraft:${item.name}`
}

function normalizeRecipe (registry, output, recipe) {
  const ingredients = {}
  for (const entry of recipe.delta) {
    if (entry.id < 0 || entry.count >= 0) continue
    const name = namespacedItemName(registry, entry.id)
    ingredients[name] = (ingredients[name] || 0) - entry.count
  }
  if (Object.keys(ingredients).length === 0) {
    throw new Error(`recipe ${output} has no consumable ingredients`)
  }
  return {
    output,
    output_count: recipe.result.count,
    ingredients,
    requires_table: recipe.requiresTable
  }
}

function loadRecipeCatalog (minecraftVersion = '1.19') {
  const registry = minecraftData(minecraftVersion)
  if (!registry || !registry.recipes || !registry.items) {
    throw new Error(`minecraft-data does not provide recipes for ${minecraftVersion}`)
  }
  const Recipe = prismarineRecipe(registry).Recipe
  const recipes = {}
  const itemStackSizes = {}
  for (const [itemId, item] of Object.entries(registry.items)) {
    if (!item || !item.name || !Number.isInteger(item.stackSize) || item.stackSize < 1) {
      throw new Error(`minecraft-data item ${itemId} has an invalid stackSize`)
    }
    itemStackSizes[namespacedItemName(registry, Number(itemId))] = item.stackSize
  }
  for (const outputId of Object.keys(registry.recipes)) {
    const numericId = Number(outputId)
    const output = namespacedItemName(registry, numericId)
    recipes[output] = Recipe.find(numericId, null).map(recipe => (
      normalizeRecipe(registry, output, recipe)
    ))
  }
  return {
    provider: 'mineflayer/minecraft-data',
    minecraft_version: registry.version.minecraftVersion,
    mineflayer_version: require('mineflayer/package.json').version,
    minecraft_data_version: require('minecraft-data/package.json').version,
    item_stack_sizes: itemStackSizes,
    recipes
  }
}

if (require.main === module) {
  try {
    process.stdout.write(JSON.stringify(loadRecipeCatalog(process.argv[2] || '1.19')))
  } catch (error) {
    process.stderr.write(`${error.stack || error.message}\n`)
    process.exitCode = 1
  }
}

module.exports = { loadRecipeCatalog }
