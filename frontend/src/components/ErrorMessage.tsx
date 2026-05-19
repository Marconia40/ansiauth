interface Props {
  error: string | null;
}

export function ErrorMessage({ error }: Props) {
  if (!error) return null;
  return <p className="text-sm text-red-600 mt-1">{error}</p>;
}
