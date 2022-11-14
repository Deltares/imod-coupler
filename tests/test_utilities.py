from pathlib import Path


def fill_para_sim_template(msw_folder: Path, path_unsat_dbase: Path) -> None:
    """
    Fill para_sim.inp template in the folder with the path to the unsaturated
    zone database.
    """
    with open(msw_folder / "para_sim_template.inp") as f:
        para_sim_text = f.read()

    para_sim_text = para_sim_text.replace("{{unsat_path}}", f"{path_unsat_dbase}\\")

    with open(msw_folder / "para_sim.inp", mode="w") as f:
        f.write(para_sim_text)
