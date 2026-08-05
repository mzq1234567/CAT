import React from "react";
import MemoryIcon from "@mui/icons-material/Memory";
import StorageIcon from "@mui/icons-material/Storage";
import StorageRoundedIcon from "@mui/icons-material/StorageRounded";
import HubIcon from "@mui/icons-material/Hub";
import BackupOutlinedIcon from "@mui/icons-material/BackupOutlined";
import CategoryIcon from "@mui/icons-material/Category";
import type { Area } from "./area";

/** Shared category → icon map, so tiles and cards use one consistent set. */
export const AREA_ICON: Record<Area, React.ReactNode> = {
  Compute: <MemoryIcon fontSize="small" />,
  Storage: <StorageIcon fontSize="small" />,
  Databases: <StorageRoundedIcon fontSize="small" />,
  Network: <HubIcon fontSize="small" />,
  Backup: <BackupOutlinedIcon fontSize="small" />,
  Other: <CategoryIcon fontSize="small" />,
};
