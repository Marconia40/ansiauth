import type { PortMode } from '@/types/port';
import { PAGE_SIZE_OPTIONS, SELECT_CLS } from '../helpers';
import type { AdminFilter, FilterBarValues, OperFilter } from '../types';

export function FilterBar(props: FilterBarValues) {
  return (
    <div className="flex flex-wrap items-center gap-2 mb-3">
      <input
        type="search"
        placeholder="Search interface or description..."
        value={props.search}
        onChange={(e) => props.setSearch(e.target.value)}
        className="px-3 py-1.5 text-sm border border-gray-300 rounded-md w-64 focus:outline-none focus:ring-1 focus:ring-blue-400"
      />

      <select
        value={props.filterMode}
        onChange={(e) => props.setFilterMode(e.target.value as PortMode | '')}
        className={SELECT_CLS}
      >
        <option value="">All modes</option>
        <option value="access">Access</option>
        <option value="trunk">Trunk</option>
        <option value="unknown">Unknown</option>
      </select>

      <select
        value={props.filterAdmin}
        onChange={(e) => props.setFilterAdmin(e.target.value as AdminFilter)}
        className={SELECT_CLS}
      >
        <option value="">Any admin state</option>
        <option value="enabled">Admin enabled</option>
        <option value="disabled">Admin disabled</option>
      </select>

      <select
        value={props.filterOper}
        onChange={(e) => props.setFilterOper(e.target.value as OperFilter)}
        className={SELECT_CLS}
      >
        <option value="">Any link state</option>
        <option value="up">Link up</option>
        <option value="down">Link down</option>
      </select>

      {props.hasActiveFilters && (
        <button onClick={props.onClear} className="text-xs text-gray-500 hover:text-gray-700 underline px-1">
          Clear
        </button>
      )}

      <div className="ml-auto flex items-center gap-1.5">
        <span className="text-xs text-gray-400 whitespace-nowrap">Per page:</span>
        <select
          value={props.pageSize}
          onChange={(e) => props.setPageSize(Number(e.target.value))}
          className={SELECT_CLS}
        >
          {PAGE_SIZE_OPTIONS.map((n) => (
            <option key={n} value={n}>
              {n}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}
