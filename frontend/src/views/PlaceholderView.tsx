import { Construction } from "lucide-react";

interface Props {
  title: string;
  desc: string;
}

export function PlaceholderView({ title, desc }: Props) {
  return (
    <div className="max-w-7xl mx-auto">
      <div className="card p-8 flex flex-col items-center text-center gap-3">
        <Construction className="w-10 h-10 text-amber-500" />
        <h2 className="text-lg font-semibold">{title}</h2>
        <p className="text-sm opacity-70 max-w-md">{desc}</p>
        <p className="text-xxs opacity-50 font-mono mt-2">
          Coming in the next migration pass. The data and API endpoints already exist —
          just needs the React view wired up.
        </p>
      </div>
    </div>
  );
}
